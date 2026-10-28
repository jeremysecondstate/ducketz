"""Offline checks for bounded, observational journal recovery diagnostics."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from ml.intraday import dataset_diagnostics as diagnostics


def groups(profile):
    return {row["group"]: row for row in profile["recovery_subphases"]["groups"]}


def test_no_capture_does_not_read_clocks_or_create_context(monkeypatch):
    def forbidden():
        raise AssertionError("No clocks outside capture")
    monkeypatch.setattr(diagnostics, "_clocks", forbidden)
    with diagnostics.recovery_phase("journal_connect", "a" * 32):
        assert diagnostics._current.get() is None
        assert diagnostics._recovery_journal.get() is None


def test_bounded_groups_samples_overflow_and_existing_phase_slots(monkeypatch):
    counter = iter(range(10000))
    monkeypatch.setattr(diagnostics, "_clocks", lambda: (float(next(counter)), 0.0, 0.0))
    with diagnostics.capture_dataset_build() as profile:
        for index in range(48):
            with diagnostics.recovery_phase("journal_connect", format(index, "032x")):
                pass
        for name in ("group_" + letter for letter in "abcdefghijklmnopqrst"):
            with diagnostics.recovery_phase(name):
                pass
        for _ in range(32):
            with diagnostics.dataset_phase("preserved"):
                pass
    assert len(groups(profile)) == 20
    assert profile["recovery_subphases"]["overflow_count"] == 1
    assert len(profile["phases"]) == 32
    row = groups(profile)["journal_connect"]
    assert row["count"] == 48 and row["error_count"] == 0
    assert len(row["slow_samples"]) == 3
    assert row["wall_seconds"] == 48 and row["maximum_wall_seconds"] == 1
    assert profile["recovery_subphases"]["totals_are_inclusive"] is True
    assert all(x["start_offset_seconds"] <= x["end_offset_seconds"] for x in row["slow_samples"])


def test_constructor_identity_nested_capture_and_restore():
    with diagnostics.capture_dataset_build() as outer:
        with diagnostics.recovery_phase("journal_constructor", "a" * 32):
            with diagnostics.recovery_phase("journal_connect"):
                pass
            with diagnostics.capture_dataset_build() as inner:
                with diagnostics.recovery_phase("journal_connect"):
                    pass
            with diagnostics.recovery_phase("journal_full_audit"):
                pass
        with diagnostics.recovery_phase("inventory_validation"):
            pass
    assert groups(inner)["journal_connect"]["slow_samples"][0]["original_journal_id"] is None
    for name in ("journal_constructor", "journal_connect", "journal_full_audit"):
        assert groups(outer)[name]["slow_samples"][0]["original_journal_id"] == "a" * 32
    assert groups(outer)["inventory_validation"]["slow_samples"][0]["original_journal_id"] is None
    assert diagnostics._current.get() is None and diagnostics._recovery_journal.get() is None


def test_thread_captures_keep_original_ids_separate():
    barrier = threading.Barrier(2)
    def worker(identity):
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.recovery_phase("journal_constructor", identity):
                barrier.wait(timeout=5)
                with diagnostics.recovery_phase("journal_connect"):
                    pass
        return profile
    with ThreadPoolExecutor(max_workers=2) as pool:
        profiles = list(pool.map(worker, ("a" * 32, "b" * 32)))
    assert [groups(p)["journal_connect"]["slow_samples"][0]["original_journal_id"] for p in profiles] == ["a" * 32, "b" * 32]


@pytest.mark.parametrize("name,identity", [("invalid/path", None), ("journal_connect", "private-path"), ("journal_connect", 7)])
def test_invalid_diagnostic_metadata_does_not_change_operation(name, identity):
    marker = object()
    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.recovery_phase(name, identity):
            result = marker
    assert result is marker and not groups(profile)
    assert profile["observer_errors"] == ["ValueError"]


@pytest.mark.parametrize("business_error", [RuntimeError("original business detail"), KeyboardInterrupt("original cancellation")])
@pytest.mark.parametrize("observer_error", [ValueError("observer"), KeyboardInterrupt("observer cancellation")])
def test_original_exception_identity_survives_observer_failure(monkeypatch, business_error, observer_error):
    def fail(*args):
        raise observer_error
    monkeypatch.setattr(diagnostics, "_finish_recovery", fail)
    with pytest.raises(type(business_error)) as caught:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.recovery_phase("journal_records", "a" * 32):
                raise business_error
    assert caught.value is business_error
    assert profile["observer_errors"] == [type(observer_error).__name__]
    assert diagnostics._current.get() is None and diagnostics._recovery_journal.get() is None


def test_observer_error_keeps_success_but_sole_cancellation_propagates(monkeypatch):
    def fail(*args):
        raise ValueError("observer")
    monkeypatch.setattr(diagnostics, "_finish_recovery", fail)
    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.recovery_phase("journal_records"):
            result = 3
    assert result == 3 and profile["status"] == "success"
    error = KeyboardInterrupt("diagnostic cancellation")
    def cancel(*args):
        raise error
    monkeypatch.setattr(diagnostics, "_finish_recovery", cancel)
    with pytest.raises(KeyboardInterrupt) as caught:
        with diagnostics.capture_dataset_build():
            with diagnostics.recovery_phase("journal_records", "a" * 32):
                pass
    assert caught.value is error
    assert diagnostics._recovery_journal.get() is None


@pytest.mark.parametrize("bad_clock", [ValueError("clock"), (float("nan"), 0, 0), (1, 2)])
def test_clock_failure_is_observational(monkeypatch, bad_clock):
    def clock():
        if isinstance(bad_clock, Exception):
            raise bad_clock
        return bad_clock
    monkeypatch.setattr(diagnostics, "_clocks", clock)
    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.recovery_phase("journal_records"):
            pass
    assert profile["status"] == "success" and not groups(profile)
    assert profile["observer_errors"] == ["ValueError"]


def test_ordinary_failed_operation_records_only_error_type():
    error = ValueError("private broker/account detail")
    with pytest.raises(ValueError) as caught:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.recovery_phase("journal_records", "a" * 32):
                raise error
    row = groups(profile)["journal_records"]
    assert caught.value is error and row["count"] == row["error_count"] == 1
    assert row["slow_samples"][0]["error_type"] == "ValueError"
    assert "private broker" not in str(profile)


@pytest.mark.parametrize("business_error", [RuntimeError("original"), KeyboardInterrupt("original")])
@pytest.mark.parametrize("failure_at", ["entry", "finish"])
def test_phase_inside_finally_preserves_pending_original(monkeypatch, business_error, failure_at):
    def cancel(*args):
        raise KeyboardInterrupt("observer")
    monkeypatch.setattr(diagnostics, "_clocks" if failure_at == "entry" else "_finish_recovery", cancel)
    closed = []
    with pytest.raises(type(business_error)) as caught:
        with diagnostics.capture_dataset_build() as profile:
            try:
                raise business_error
            finally:
                with diagnostics.recovery_phase("journal_close"):
                    closed.append(True)
    assert caught.value is business_error and closed == [True]
    assert profile["observer_errors"] == ["KeyboardInterrupt"]
    assert diagnostics._current.get() is None and diagnostics._recovery_journal.get() is None


def test_journal_identity_lookup_is_deferred_and_observational():
    class Journal:
        @property
        def journal_id(self):
            raise ValueError("unavailable diagnostic identity")
    journal = Journal()
    with diagnostics.recovery_phase("journal_records", journal=journal):
        pass
    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.recovery_phase("journal_records", journal=journal):
            result = 3
    assert result == 3 and profile["observer_errors"] == ["ValueError"]
    assert not groups(profile)


def test_deferred_journal_identity_retains_constructor_original():
    class Journal:
        journal_id = None
    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.recovery_phase("journal_constructor", "a" * 32):
            with diagnostics.recovery_phase("journal_connect", journal=Journal()):
                pass
    assert groups(profile)["journal_connect"]["slow_samples"][0]["original_journal_id"] == "a" * 32
