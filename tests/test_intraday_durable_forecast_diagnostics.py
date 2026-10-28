"""Portable synthetic outcome checks; no account or provider sources."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_intraday_partial_header_reads import (
    cache, inventory, offline, pin_reader, expected_prefix)


FIELDS = {"schema_version", "reason", "requested_count", "expected_count",
          "validated_count", "returned_count", "discarded_count", "rejected_count",
          "sqlite_errorcode"}


def read(inventory, report):
    return inventory.helper.load_many(inventory.identity, inventory.signatures,
                                      diagnostics=report)


def frozen(inventory):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (inventory.source, inventory.helper.path, inventory.helper.key_path)}


def assert_report(report, reason, *, validated=0, returned=0, discarded=0, rejected=0):
    assert set(report) == FIELDS
    assert report["schema_version"] == "forecast-cache-load-outcome-v1"
    assert report["reason"] == reason
    assert report["validated_count"] == validated
    assert report["returned_count"] == returned
    assert report["discarded_count"] == discarded
    assert report["rejected_count"] == rejected
    assert all(type(value) is int and 0 <= value <= 2147483647
               for name, value in report.items() if name.endswith("_count"))
    encoded = json.dumps(report, allow_nan=False)
    for private in ("forecast-000", "intention-", "synthetic-source-epoch", "calculated-", "database is locked"):
        assert private not in encoded


@pytest.mark.parametrize("after", [0, 1, 17, 400, 866])
def test_real_interrupt_reports_preserved_authenticated_prefix(monkeypatch, inventory, after):
    before = frozen(inventory)
    state = pin_reader(monkeypatch, inventory, after=after)
    report = {"preexisting": "must be replaced"}
    assert read(inventory, report) == expected_prefix(inventory, state)
    assert_report(report, "sqlite_interrupt", validated=after, returned=after)
    assert report["sqlite_errorcode"] == sqlite3.SQLITE_INTERRUPT
    assert report["requested_count"] == report["expected_count"] == 867
    assert frozen(inventory) == before and state.closed


@pytest.mark.parametrize("failure,reason,rejected", [
    ("busy", "sqlite_error", 0), ("corrupt", "sqlite_error", 0),
    ("generic_operational", "sqlite_error", 0), ("io", "os_error", 0),
    ("value", "invalid_record", 1)])
def test_other_errors_report_discarded_prefix_without_releasing_it(monkeypatch, inventory, failure, reason, rejected):
    before = frozen(inventory)
    state = pin_reader(monkeypatch, inventory, failure=failure)
    report = {}
    assert read(inventory, report) == {}
    assert_report(report, reason, validated=17, discarded=17, rejected=rejected)
    assert report["sqlite_errorcode"] is None
    assert frozen(inventory) == before and state.closed


def test_complete_and_duplicate_request_counts_without_extra_queries(monkeypatch, inventory):
    inventory.signatures += inventory.signatures[:2]
    before = frozen(inventory)
    state = pin_reader(monkeypatch, inventory, after=900)
    report = {}
    result = read(inventory, report)
    assert result == {v[0]: v[1] for v in inventory.entries.values()}
    assert_report(report, "complete", validated=867, returned=867)
    assert report["requested_count"] == 869 and report["expected_count"] == 867
    assert frozen(inventory) == before and state.closed


@pytest.mark.parametrize("case,reason", [("disabled", "budget_disabled"),
    ("missing_file", "missing_file"), ("missing_key", "missing_key"),
    ("context", "invalid_context"), ("empty", "empty")])
def test_early_return_reasons_preserve_original_fallback(monkeypatch, inventory, case, reason):
    if case == "disabled":
        inventory.helper._remaining = 0
    elif case == "missing_file":
        monkeypatch.setattr(cache.Path, "is_file", lambda self: False)
    elif case == "missing_key":
        monkeypatch.setattr(inventory.helper, "_key_material", lambda: None)
    elif case == "context":
        monkeypatch.setattr(inventory.helper, "_context", lambda db: None)
    else:
        inventory.signatures = []
    before = frozen(inventory)
    report = {}
    assert read(inventory, report) == {}
    assert_report(report, reason)
    assert report["requested_count"] == (0 if case == "empty" else 867)
    assert frozen(inventory) == before


def test_budget_reports_retained_prefix_with_original_clock_count(monkeypatch, inventory):
    state = pin_reader(monkeypatch, inventory, after=900)
    clock = SimpleNamespace(now=0., calls=0)
    def now():
        clock.calls += 1
        return clock.now
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=now))
    authenticate = inventory.helper._authenticate
    def measured(secret, raw):
        result = authenticate(secret, raw)
        if len(state.yielded) == 17:
            clock.now = 5.
        return result
    monkeypatch.setattr(inventory.helper, "_authenticate", measured)
    report = {}
    result = read(inventory, report)
    assert len(result) == 17
    assert_report(report, "budget_exhausted", validated=17, returned=17)
    assert clock.calls == 20  # start +18 row checks +existing final budget debit
    assert inventory.helper._remaining == 0 and state.closed


def test_rejected_mac_is_counted_without_changing_valid_subset(monkeypatch, inventory):
    def changed(index, row):
        return (row[0], row[1], b"X" * 32) if index == 3 else row
    state = pin_reader(monkeypatch, inventory, after=900, transform=changed)
    report = {}
    result = read(inventory, report)
    assert len(result) == 866
    assert_report(report, "invalid_record", validated=866, returned=866, rejected=1)
    assert state.closed


def test_authenticated_malformed_json_reports_discarded_prefix(monkeypatch, inventory):
    def malformed(index, row):
        if index != 3:
            return row
        raw = b"synthetic malformed JSON"
        return row[0], raw, cache.DurableForecastHeaders._authenticate(inventory.secret, raw)
    state = pin_reader(monkeypatch, inventory, after=900, transform=malformed)
    report = {}
    assert read(inventory, report) == {}
    assert_report(report, "invalid_record", validated=3, discarded=3, rejected=1)
    assert state.closed


@pytest.mark.parametrize("output", [None, [], 17, SimpleNamespace(), type("BadDict", (dict,), {
    "clear": lambda self: (_ for _ in ()).throw(RuntimeError("diagnostics unavailable"))})()])
def test_unusable_diagnostic_output_never_changes_original_result(monkeypatch, inventory, output):
    state = pin_reader(monkeypatch, inventory, after=17)
    assert read(inventory, output) == expected_prefix(inventory, state)
    assert state.closed


@pytest.mark.parametrize("errorcode,expected", [(5,5), (sqlite3.SQLITE_BUSY | 256,sqlite3.SQLITE_BUSY | 256),
    (-1,None), (True,None), (2**63,None), ("5",None)])
def test_sqlite_error_codes_are_bounded_without_error_text(monkeypatch, inventory, errorcode, expected):
    def failure():
        error = sqlite3.OperationalError("PRIVATE path key and account text")
        error.sqlite_errorcode = errorcode
        raise error
    monkeypatch.setattr(inventory.helper, "_connect", failure)
    report = {}
    assert read(inventory, report) == {}
    assert_report(report, "sqlite_error")
    assert report["sqlite_errorcode"] == expected
    assert "PRIVATE" not in json.dumps(report)
