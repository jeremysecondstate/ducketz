"""Offline recovery clocks preserve exact own-attempt projections and failures."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from ml.intraday import account_execution as execution
from ml.intraday import intentions as module
from ml.intraday.prediction import digest, encoded
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_recovery_grouping import observation, projection_rows
from tests.test_intraday_recovery_sizing_projection import prepared
from tests.test_intraday_store import forecast


PHASES = {"group_total", "writer_lock", "forecast_read", "forecast_validate",
          "sizing_read", "sizing_validate", "projection_append"}


def delayed_stages(store, intents, monkeypatch):
    """Advance a local diagnostic clock at actual transaction/validation seams."""
    ticks = [0.]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    connect = store.connect

    class Cursor:
        def __init__(self, cursor, delay):
            self.cursor, self.delay = cursor, delay

        def fetchone(self):
            ticks[0] += self.delay
            return self.cursor.fetchone()

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def __iter__(self):
            return iter(self.cursor)

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                ticks[0] += 3
            delay = (5 if "FROM forecasts WHERE id=?" in sql else
                     7 if "FROM intraday_intentions WHERE forecast_id=?" in sql else 0)
            return Cursor(self.db.execute(sql, *args), delay)

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def delayed_connect():
        ticks[0] += 2
        with connect() as db:
            yield Connection(db)
        # Includes successful transaction exit/commit and connection cleanup.
        ticks[0] += 11

    monkeypatch.setattr(store, "connect", delayed_connect)
    for owner, name, delay in ((store, "_forecast_projection", 13),
                               (intents, "_recovery_sizing", 17),
                               (intents, "_append_validated", 19)):
        original = getattr(owner, name)

        def delayed(*args, _original=original, _delay=delay, **kwargs):
            ticks[0] += _delay
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, delayed)
    return ticks


def sample(count, total, maximum=None):
    return {"count": count, "total_seconds": total,
            "maximum_seconds": total if maximum is None else maximum}


def test_phase_clocks_separate_writer_reads_validation_append_and_commit(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    filled = replace(order, reservation_id="filled-attempt", status="FILLED",
                     broker_order_id="broker-original", filled_quantity=order.quantity)
    unknown = replace(order, reservation_id="unknown-attempt", status="UNKNOWN")
    orders = (order, filled, unknown)
    items = tuple(observation(value) for value in orders)
    expected = intents.record_recovery_group(original["forecast_id"], actor="Atlas", observations=items)
    before = projection_rows(store)
    original_forecasts = store.forecasts()
    store._execution_forecast_cache.clear()
    store._forecast_header_cache.clear()
    intents._recovery_row_cache.clear()
    delayed_stages(store, intents, monkeypatch)
    timings = module._RecoveryTimings()

    actual = intents.record_recovery_group(original["forecast_id"], actor="Atlas",
                                           observations=items, timings=timings)

    assert actual == expected
    assert projection_rows(store) == before
    assert store.forecasts() == original_forecasts
    assert [row["order"] for row in actual] == [asdict(value) for value in orders]
    assert actual[1]["broker_order_id"] == "broker-original"
    assert actual[1]["submitted_quantity"] == actual[1]["filled_quantity"] == order.quantity
    assert actual[2]["submitted_quantity"] is None and actual[2]["filled_quantity"] == 0
    assert len({row["event_identity"] for row in actual}) == 3
    assert {key: value for key, value in timings.snapshot().items() if key != "read_details"} == {
        "schema_version": "intraday-recovery-phase-timings-v1",
        "phases": {"group_total": sample(1, 128.), "writer_lock": sample(1, 3.),
                   # Both read and final writer checks are timed; protected header lookup includes cold semantic validation;
                   # forecast_validate is the subsequent local actor check.
                   "forecast_read": sample(2, 18.), "forecast_validate": sample(2, 0.),
                   "sizing_read": sample(2, 7.), "sizing_validate": sample(2, 17.),
                   "projection_append": sample(1, 57.)},
        "diagnostics_unavailable": False,
    }
    warm = module._RecoveryTimings()
    assert intents.record_recovery_group(original["forecast_id"], actor="Atlas",
        observations=items, timings=warm) == expected
    phases = warm.snapshot()["phases"]
    assert phases["group_total"] == sample(1, 86.)
    assert all(phases[name] == sample(2, 0.) for name in
               ("forecast_read", "forecast_validate", "sizing_read", "sizing_validate"))


def test_many_groups_aggregate_fixed_phase_keys_and_return_detached_snapshots(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    # Construction's ordinary sizing receipt is outside these diagnostic clocks.
    intents = module.IntentionStore(store)
    delayed_stages(store, intents, monkeypatch)
    timings = module._RecoveryTimings()
    item = observation(order)
    expected = None
    for _ in range(32):
        actual = intents.record_recovery_group(original["forecast_id"], actor="Atlas",
                                               observations=(item,), timings=timings)
        expected = actual if expected is None else expected
        assert actual == expected

    saved = timings.snapshot()
    assert set(saved["phases"]) == PHASES
    assert saved["phases"]["group_total"] == sample(32, 90. + 31 * 48., 90.)
    assert saved["phases"]["projection_append"] == sample(32, 32 * 19., 19.)
    assert all(value["count"] == (64 if name in {"forecast_read", "forecast_validate", "sizing_read", "sizing_validate"} else 32)
               for name, value in saved["phases"].items())
    assert not saved["diagnostics_unavailable"]
    saved["phases"]["writer_lock"]["count"] = -100
    saved["phases"]["unexpected"] = {"private": "not retained"}
    fresh = timings.snapshot()
    assert set(fresh["phases"]) == PHASES
    assert fresh["phases"]["writer_lock"] == sample(32, 96., 3.)
    assert "private" not in json.dumps(fresh)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                  "invalid-clock", None, RuntimeError("clock unavailable")])
def test_bad_clock_does_not_change_success_or_projection_bytes(tmp_path, monkeypatch, bad):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    expected = intents.record_recovery_group(original["forecast_id"], actor="Atlas", observations=(item,))
    before = projection_rows(store)

    def clock():
        if isinstance(bad, Exception):
            raise bad
        return bad

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock))
    timings = module._RecoveryTimings()
    assert intents.record_recovery_group(original["forecast_id"], actor="Atlas",
                                         observations=(item,), timings=timings) == expected
    assert projection_rows(store) == before
    saved = timings.snapshot()
    assert saved["diagnostics_unavailable"] is True
    json.dumps(saved, allow_nan=False)


@pytest.mark.parametrize("clock_values", [(10., 5.), (0., float("nan")),
                                           (float("inf"), 1.)])
def test_invalid_elapsed_time_is_unavailable_and_preserves_operation_exception(monkeypatch, clock_values):
    values = iter(clock_values)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(values)))
    timings = module._RecoveryTimings()
    failure = RuntimeError("actual operation failure")
    with pytest.raises(RuntimeError) as raised:
        with timings.measure("writer_lock"):
            raise failure
    assert raised.value is failure
    saved = timings.snapshot()
    assert saved["diagnostics_unavailable"] is True
    json.dumps(saved, allow_nan=False)


def test_aggregate_overflow_preserves_prior_finite_sample_and_operation_exception(monkeypatch):
    values = iter((0., 1e308, 0., 1e308))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(values)))
    timings = module._RecoveryTimings()
    operations = []
    with timings.measure("writer_lock"):
        operations.append("first operation completed")
    failure = RuntimeError("second operation failure")
    with pytest.raises(RuntimeError) as raised:
        with timings.measure("writer_lock"):
            operations.append("second operation attempted")
            raise failure
    assert operations == ["first operation completed", "second operation attempted"]
    assert raised.value is failure
    saved = timings.snapshot()
    assert saved["phases"] == {"writer_lock": sample(1, 1e308)}
    assert saved["diagnostics_unavailable"] is True
    json.dumps(saved, allow_nan=False)


def test_failed_group_preserves_original_exception_and_rolls_back_with_failed_clock(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    before = projection_rows(store)
    failure = RuntimeError("original forecast validation failure")

    def fail_validation(*args, **kwargs):
        raise failure

    def fail_clock():
        raise RuntimeError("diagnostic clock failure")

    monkeypatch.setattr(store, "_forecast_projection", fail_validation)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=fail_clock))
    timings = module._RecoveryTimings()
    with pytest.raises(RuntimeError) as raised:
        intents.record_recovery_group(original["forecast_id"], actor="Atlas",
                                      observations=(observation(order),), timings=timings)
    assert raised.value is failure
    assert projection_rows(store) == before
    assert timings.snapshot()["diagnostics_unavailable"] is True


def test_executor_shares_one_aggregate_across_groups_without_changing_attempt_order(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    other = store.save_forecast(forecast(cycle_id="another-original"))
    value = dict(forecast_id=other["forecast_id"], symbol="AAPL", side="BUY", status="sized",
                 desired_quantity=4, calculated_at="2026-10-08T17:16:03Z")
    intents.save_sizing(other, SizingResult(digest(value), encoded(value)))
    owned = (order, replace(order, reservation_id="other-attempt", forecast_id=other["forecast_id"]),
             replace(order, reservation_id="filled-attempt", status="FILLED",
                     broker_order_id="actual-original-order", filled_quantity=order.quantity))
    expected_by_forecast = {}
    for current in (original, other):
        members = tuple(observation(row) for row in owned if row.forecast_id == current["forecast_id"])
        expected_by_forecast[current["forecast_id"]] = intents.record_recovery_group(
            current["forecast_id"], actor="Atlas", observations=members)
    expected = [expected_by_forecast[original["forecast_id"]][0],
                expected_by_forecast[other["forecast_id"]][0],
                expected_by_forecast[original["forecast_id"]][1]]
    before = projection_rows(store)
    original_forecasts = store.forecasts()
    delayed_stages(store, intents, monkeypatch)
    trace = execution._ExecutionTrace()
    assert trace.recovery_timings is None
    assert "recovery_phase_timings" not in trace.snapshot()

    @contextmanager
    def transaction(*args, **kwargs):
        yield None

    class NoBrokerCalls:
        def __getattr__(self, name):
            pytest.fail("Recovery projections attempted broker access: " + name)

    executor = object.__new__(execution.IntradayAccountExecutor)
    executor._account = transaction
    executor._capture = lambda: None
    executor._execution_trace = trace
    executor.ledger = SimpleNamespace(_transaction=transaction)
    executor._owned_native_reservations = lambda db: owned
    executor._execution_stage = lambda stage: None
    executor.actor = "Atlas"
    executor.clock = lambda: "2026-10-08T17:17:00Z"
    executor.intents = intents
    executor.broker = NoBrokerCalls()
    calls = []
    grouped = intents.record_recovery_group

    def tracked(forecast_id, **kwargs):
        calls.append((forecast_id, kwargs.get("timings")))
        return grouped(forecast_id, **kwargs)

    monkeypatch.setattr(intents, "record_recovery_group", tracked)
    actual = executor.recover()
    assert actual == expected
    assert projection_rows(store) == before
    assert store.forecasts() == original_forecasts
    assert [row["order"] for row in actual] == [asdict(row) for row in owned]
    assert [row["evidence"]["native_reservation_id"] for row in actual] == [row.reservation_id for row in owned]
    assert [row[0] for row in calls] == [original["forecast_id"], other["forecast_id"]]
    assert all(row[1] is trace.recovery_timings for row in calls)
    assert trace.recovery_timings is not None
    phases = trace.snapshot()["recovery_phase_timings"]["phases"]
    assert set(phases) == PHASES
    assert phases["group_total"] == sample(2, 115., 67.)
    assert phases["projection_append"] == sample(2, 57., 38.)
    assert all(value["count"] == (4 if name in {"forecast_read", "forecast_validate", "sizing_read", "sizing_validate"} else 2)
               for name, value in phases.items())


def clear_original_caches(store, intents):
    store._execution_forecast_cache.clear()
    getattr(store, "_forecast_header_cache", {}).clear()
    intents._recovery_row_cache.clear()


def test_read_details_separate_actual_cold_reads_validation_and_warm_cache(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    clear_original_caches(store, intents)
    with store.connect() as db:
        forecast_raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (original["forecast_id"],)).fetchone()[0]
        sizing_raw = db.execute("SELECT payload FROM intraday_intentions WHERE forecast_id=?", (original["forecast_id"],)).fetchone()[0]
    delayed_stages(store, intents, monkeypatch)
    cold, warm = module._RecoveryTimings(), module._RecoveryTimings()
    expected = intents.record_recovery_group(original["forecast_id"], actor="Atlas",
        observations=(observation(order),), timings=cold)
    actual = intents.record_recovery_group(original["forecast_id"], actor="Atlas",
        observations=(observation(order),), timings=warm)
    assert actual == expected
    details = cold.snapshot()["read_details"]
    assert set(details["phases"]) == module._RecoveryTimings._DETAILS
    assert details["phases"]["forecast_payload_read"] == sample(1, 5.)
    assert details["phases"]["forecast_payload_validate"] == sample(1, 13.)
    assert details["phases"]["sizing_payload_read"] == sample(1, 7.)
    assert details["phases"]["sizing_payload_validate"] == sample(1, 17.)
    assert details["phases"]["preflight_connection"] == sample(1, 55.)
    assert details["phases"]["writer_connection"] == sample(1, 35.)
    assert details["workload"] == dict(forecast_cache_misses=1, forecast_cache_hits=1,
        forecast_payload_reads=1, forecast_text_characters=len(forecast_raw),
        sizing_cache_misses=1, sizing_cache_hits=1, sizing_payload_reads=1,
        sizing_text_characters=len(sizing_raw))
    warmed = warm.snapshot()["read_details"]
    assert warmed["workload"] == {"forecast_cache_hits": 2, "sizing_cache_hits": 2}
    assert all("payload" not in stage for stage in warmed["phases"])
    assert not details["diagnostics_unavailable"]
    # This fixture intentionally has no thread CPU clock; wall clocks still work.
    assert details["thread_cpu_unavailable"]
    details["workload"]["sizing_cache_hits"] = -1
    assert cold.snapshot()["read_details"]["workload"]["sizing_cache_hits"] == 1


def test_diagnostics_add_no_queries_and_preserve_exact_projection(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    connect, statements = store.connect, []

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced)
    item = observation(order)
    # First create the idempotent result; both compared passes then do the same work.
    expected = intents.record_recovery_group(original["forecast_id"], actor="Atlas", observations=(item,))
    clear_original_caches(store, intents)
    statements.clear()
    assert intents.record_recovery_group(original["forecast_id"], actor="Atlas", observations=(item,)) == expected
    without_diagnostics = list(statements)
    before = projection_rows(store)
    clear_original_caches(store, intents)
    statements.clear()
    timings = module._RecoveryTimings()
    assert intents.record_recovery_group(original["forecast_id"], actor="Atlas", observations=(item,), timings=timings) == expected
    assert statements == without_diagnostics
    assert projection_rows(store) == before
    assert not timings.snapshot()["read_details"]["thread_cpu_unavailable"]


def test_thread_cpu_and_elapsed_measure_distinct_actual_validation_work(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    clear_original_caches(store, intents)
    ticks, cpu = [0.], [0.]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: ticks[0], thread_time=lambda: cpu[0]))
    validate = intents._recovery_sizing

    def delayed(row):
        ticks[0] += 11
        cpu[0] += 2
        return validate(row)

    monkeypatch.setattr(intents, "_recovery_sizing", delayed)
    timings = module._RecoveryTimings()
    intents.record_recovery_group(original["forecast_id"], actor="Atlas", observations=(observation(order),), timings=timings)
    details = timings.snapshot()["read_details"]
    assert details["phases"]["sizing_payload_validate"] == {
        **sample(1, 11.), "thread_cpu_count": 1, "thread_cpu_total_seconds": 2., "thread_cpu_maximum_seconds": 2.}
    assert not details["thread_cpu_unavailable"] and not details["diagnostics_unavailable"]


@pytest.mark.parametrize("clock", ["monotonic", "thread_time"])
@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "invalid", RuntimeError("private diagnostic failure")])
def test_failed_detail_clock_cannot_mask_original_operation_exception(monkeypatch, clock, bad):
    def failed():
        if isinstance(bad, Exception):
            raise bad
        return bad

    clocks = {"monotonic": lambda: 0., "thread_time": lambda: 0.}
    clocks[clock] = failed
    monkeypatch.setattr(module, "time", SimpleNamespace(**clocks))
    timings = module._RecoveryTimings()
    original = RuntimeError("original operation error")
    with pytest.raises(RuntimeError) as raised:
        with timings.detail("sizing_index_read"):
            raise original
    assert raised.value is original
    details = timings.snapshot()["read_details"]
    assert details["thread_cpu_unavailable" if clock == "thread_time" else "diagnostics_unavailable"]
    assert "private" not in json.dumps(details, allow_nan=False)


def test_workload_keys_are_fixed_and_text_counts_do_not_encode_payloads():
    timings = module._RecoveryTimings()

    class Text(str):
        def encode(self, *args, **kwargs):
            pytest.fail("Diagnostics must not encode an existing payload to count it")

    timings.payload("forecast_", Text("\u00e9\u00e9"))
    timings.payload("sizing_", b"\xc3\xa9")
    timings.count("private-unbounded-key")
    timings.count("forecast_cache_hits", -1)
    details = timings.snapshot()["read_details"]
    assert details["workload"] == dict(forecast_payload_reads=1, forecast_text_characters=2,
        sizing_payload_reads=1, sizing_blob_bytes=2)
    assert details["diagnostics_unavailable"]
    assert "private" not in json.dumps(details)
