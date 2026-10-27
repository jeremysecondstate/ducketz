"""Offline Gameplan continuation: complete-universe passes and durable cursors."""
from __future__ import annotations

import json
import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

_adapter = Path(__file__).resolve().parents[1] / "ml/stock_trader/independent_session.py"
_definitions = ast.parse(_adapter.read_bytes()).body if _adapter.is_file() else []
if not any(isinstance(node, ast.FunctionDef) and node.name == "run_independent_stock_session"
           and any(arg.arg == "renewal" for arg in [*node.args.args, *node.args.kwonlyargs])
           for node in _definitions):
    pytest.skip("Installed Gameplan renewal adapter required; apply the reviewed compatibility patch",
                allow_module_level=True)

from test_independent_stock_runtime import environment, ACCOUNT
from test_gameplan_direction_runtime import prepare
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, OrderEvidence
from ml.stock_trader import independent_session as worker
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


class Clock:
    def __init__(self, value="2026-10-26T11:01:00Z"):
        self.now = pd.Timestamp(value)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += pd.Timedelta(seconds=seconds)


class Renewal:
    session_id = "offline-manual-gameplan-session"

    def __init__(self):
        self.state = {}
        self.generation = 0
        self.stop = False
        self.requests = []

    def stopped(self):
        return self.stop

    def checkpoint(self, state, next_poll_at):
        # The continuation must work with JSON bytes, not in-memory objects.
        self.state = json.loads(json.dumps(state))
        self.requests.append((self.state.copy(), pd.Timestamp(next_poll_at)))

    def successor(self, clock, *, import_seconds=0):
        self.generation += 1
        clock.now = max(clock(), self.requests[-1][1]) + pd.Timedelta(seconds=import_seconds)


def result(status="NO_ORDERS_SUBMITTED", submitted=0, **extra):
    value = {"status": status, "submitted_orders": submitted, **extra}
    return SimpleNamespace(submitted_orders=submitted, to_dict=lambda: value)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    clock, renewal = Clock(), Renewal()
    control = {"active": True}
    preflights = []
    monkeypatch.setattr(worker, "_has_inventory", lambda _: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent",
                        lambda _: SimpleNamespace(active=control["active"]))
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight",
                        lambda *a, **kw: preflights.append(kw) or {"status": "READY"})
    reports = []

    def run(runner, **options):
        return worker.run_independent_stock_session(
            tmp_path, execute=False, clock=clock, sleep=clock.sleep,
            runner=runner, reporter=reports.append, renewal=renewal,
            sizing_policy=GAMEPLAN_SIZING_POLICY, **options)

    return SimpleNamespace(root=tmp_path, clock=clock, renewal=renewal, control=control,
                           preflights=preflights, reports=reports, run=run)


def test_consecutive_complete_universe_passes_preserve_one_session_and_absolute_cadence(isolated):
    env = isolated
    passes = []
    symbols = ("AAPL", "AMZN", "GOOG")

    def runner(root, **kwargs):
        assert kwargs["entries"] and kwargs["session_managed"]
        # A pass handles its entire universe before it permits a checkpoint.
        passes.append((env.clock(), tuple((s, h) for s in symbols for h in ("1h", "4h", "1d", "1w"))))
        env.clock.sleep(2)
        return result(broker_state_capture={"status": "CURRENT"})

    for generation in range(4):
        actual = env.run(runner)
        assert actual == {"status": "PLANNED_RENEWAL", "calls": generation + 1, "orders_submitted": 0}
        assert len(env.renewal.requests) == generation + 1
        assert not (env.root / "locks/independent-stock-session.lock").exists()
        assert all(len(symbols_and_horizons) == 12 for _, symbols_and_horizons in passes)
        status = json.loads((env.root / "state/independent-stock-trader/session-status.json").read_text())
        assert status["status"] == "PLANNED_RENEWAL"
        assert status["session_id"] == env.renewal.session_id
        assert status["worker_generation"] == generation
        assert status["started_at"] == "2026-10-26T11:01:00+00:00"
        env.renewal.successor(env.clock)

    assert [at.second for at, _ in passes] == [0, 32, 4, 36]
    assert len(env.preflights) == 1
    assert sum('"status": "SESSION_STARTED"' in report for report in env.reports) == 1
    assert sum('"status": "SESSION_CONTINUED"' in report for report in env.reports) == 3


def test_partial_batch_refresh_finishes_remaining_symbols_before_renewal(isolated):
    env = isolated
    seen = []

    def runner(root, **kwargs):
        seen.append((env.renewal.generation, env.clock()))
        if len(seen) == 1:
            return result("BROKER_STATE_REFRESH_REQUIRED", 1, broker_state_capture={"status": "CURRENT"})
        return result("ORDERS_SUBMITTED", 2, broker_state_capture={"status": "CURRENT"})

    actual = env.run(runner)
    assert actual == {"status": "PLANNED_RENEWAL", "calls": 2, "orders_submitted": 3}
    assert len(env.renewal.requests) == 1
    assert len(seen) == 2 and seen[0] == seen[1]
    assert env.renewal.state["last_cycle"]["status"] == "ORDERS_SUBMITTED"


def test_five_second_inventory_cadence_and_original_session_close_survive_import_delay(isolated, monkeypatch):
    env = isolated
    monkeypatch.setattr(worker, "_has_inventory", lambda _: True)
    env.clock.now = pd.Timestamp("2026-10-26T23:59:50Z")
    calls = []

    def runner(root, **kwargs):
        calls.append(env.clock())
        return result()

    assert env.run(runner)["status"] == "PLANNED_RENEWAL"
    assert env.renewal.requests[-1][1] == pd.Timestamp("2026-10-26T23:59:55Z")
    env.renewal.successor(env.clock, import_seconds=2)
    assert env.run(runner)["status"] == "PLANNED_RENEWAL"
    assert env.renewal.requests[-1][1] == pd.Timestamp("2026-10-27T00:00:00Z")
    env.renewal.successor(env.clock)
    assert env.run(runner)["status"] == "SESSION_FINISHED"
    assert len(calls) == 2 and len(env.renewal.requests) == 2
    assert env.renewal.state["closing"] == "2026-10-27T00:00:00+00:00"
    # Even if resumed incorrectly on another day, the original close wins.
    env.clock.now += pd.Timedelta(days=1)
    assert env.run(runner)["status"] == "SESSION_FINISHED"
    assert len(calls) == 2


def test_wait_for_open_preserves_original_exchange_date_and_defers_preflight(isolated, monkeypatch):
    env = isolated
    env.clock.now = pd.Timestamp("2026-10-26T10:59:45Z")
    runner = lambda *a, **kw: pytest.fail("No broker work before the initial entry time")
    assert env.run(runner, wait_for_open=True)["status"] == "PLANNED_RENEWAL"
    assert not env.preflights
    assert env.renewal.requests[-1][1] == pd.Timestamp("2026-10-26T11:00:00Z")
    original_close = env.renewal.state["closing"]
    env.renewal.successor(env.clock)
    monkeypatch.setattr(worker, "_next_supported_session_bounds",
                        lambda *a: pytest.fail("A successor must retain the initial session calendar"))
    assert env.run(runner, wait_for_open=True)["status"] == "PLANNED_RENEWAL"
    assert len(env.preflights) == 1 and env.renewal.state["closing"] == original_close


@pytest.mark.parametrize("phase", ["before_worker", "before_runner", "after_runner"])
def test_manual_stop_prevents_work_or_handoff_at_each_boundary(isolated, phase):
    env = isolated
    calls = []
    if phase == "before_worker":
        env.renewal.stop = True
    elif phase == "before_runner":
        # Preflight is local work; a stop arriving during it must be decisive.
        env.preflights.clear()
        original = env.renewal.stopped
        env.renewal.stopped = lambda: bool(env.preflights) or original()

    def runner(root, **kwargs):
        calls.append(True)
        env.renewal.stop = True
        return result("ORDERS_SUBMITTED", 1)

    actual = env.run(runner)
    assert actual["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
    assert len(calls) == int(phase == "after_runner")
    assert not env.renewal.requests


def test_existing_manual_switch_after_a_pass_blocks_successor(isolated):
    env = isolated
    assert env.run(lambda *a, **kw: result("ORDERS_SUBMITTED", 2))["status"] == "PLANNED_RENEWAL"
    env.renewal.successor(env.clock)
    env.control["active"] = False
    actual = env.run(lambda *a, **kw: pytest.fail("Stopped session cannot execute again"))
    assert actual["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
    assert actual["calls"] == 1 and actual["orders_submitted"] == 2
    assert len(env.renewal.requests) == 1


def test_failure_counters_survive_renewal_and_unknown_exceptions_do_not_renew(isolated):
    env = isolated
    env.run(lambda *a, **kw: result("SUBMISSION_STOPPED_AFTER_ERROR", error="Simulated ambiguous POST"))
    assert env.renewal.state["failed_cycles"] == env.renewal.state["consecutive_failures"] == 1
    env.renewal.successor(env.clock)
    env.run(lambda *a, **kw: result("NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL"))
    assert env.renewal.state["failed_cycles"] == env.renewal.state["consecutive_failures"] == 1
    env.renewal.successor(env.clock)

    def crash(*args, **kwargs):
        raise ValueError("simulated unknown failure")

    with pytest.raises(ValueError, match="simulated unknown failure"):
        env.run(crash)
    assert len(env.renewal.requests) == 2
    status = json.loads((env.root / "state/independent-stock-trader/session-status.json").read_text())
    assert status["status"] == "FAILED" and status["failed_cycles"] == 2


def test_once_only_quote_recovery_does_not_replay_in_successor(isolated, monkeypatch):
    env = isolated
    options = {"resume_quote_run": "retained-run", "resume_quote_symbol": "AAPL"}
    recovery_loads, calls = [], []
    monkeypatch.setattr("ml.stock_trader.quote_recovery.load_quote_recovery",
                        lambda *a, **kw: recovery_loads.append(kw))

    def runner(root, **kwargs):
        calls.append(kwargs)
        return result()

    env.run(runner, **options)
    env.renewal.successor(env.clock)
    env.run(runner, **options)
    assert len(recovery_loads) == 1
    assert calls[0]["resume_quote_run"] == "retained-run"
    assert "resume_quote_run" not in calls[1]
    assert env.renewal.state["recovery_pending"] is False


def test_continuation_rejects_changed_local_binding_or_options(isolated):
    env = isolated
    env.run(lambda *a, **kw: result())
    env.renewal.successor(env.clock)
    env.renewal.state["root"] = str(env.root / "other-trader")
    with pytest.raises(ValueError, match="original manual session"):
        env.run(lambda *a, **kw: pytest.fail("Binding mismatch cannot execute"))


@pytest.mark.parametrize("outcome", ["WORKING", "PARTIAL", "UNKNOWN"])
def test_real_runtime_reopens_durable_orders_without_resubmission_or_renewal_cancellation(
        environment, monkeypatch, outcome):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    env.broker.unknown_submission = outcome == "UNKNOWN"
    renewal = Renewal()
    clock = Clock(env.now)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent",
                        lambda _: SimpleNamespace(active=env.active))
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight",
                        lambda *a, **kw: {"status": "READY"})
    completed = []

    def runner(root, **kwargs):
        env.now = clock()
        value = runtime.run_independent_stock_trader_once(root, session=env.broker, **kwargs)
        completed.append(value)
        return value

    def generation():
        return worker.run_independent_stock_session(
            env.root, execute=True, clock=clock, sleep=clock.sleep, runner=runner,
            reporter=lambda _: None, renewal=renewal, sizing_policy=GAMEPLAN_SIZING_POLICY)

    assert generation()["status"] == "PLANNED_RENEWAL"
    first = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations[0]
    assert len(env.broker.submissions) == 1 and first.quantity >= 2
    original_history = tuple(HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).history())
    renewal.successor(clock)
    env.now = clock()
    filled = 1 if outcome == "PARTIAL" else 0
    if outcome != "UNKNOWN":
        evidence = OrderEvidence(
            "renewal-simulated-broker-evidence", first.reservation_id, ACCOUNT, env.now.isoformat(),
            first.broker_order_id, outcome, first.quantity, filled, first.quantity - filled,
            (FillEvidence("renewal-original-fill", 1, 100., env.now.isoformat()),) if filled else ())
        env.held["AAPL"] += filled
        monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence",
                            lambda *a, **kw: (evidence,))
    assert generation()["status"] == "PLANNED_RENEWAL"
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    saved = ledger.snapshot().reservations[0]
    assert saved.reservation_id == first.reservation_id
    assert saved.forecast_id == first.forecast_id and saved.idempotency_key == first.idempotency_key
    assert saved.batch_id == first.batch_id and saved.broker_order_id == first.broker_order_id
    assert saved.status == outcome and saved.filled_quantity == filled
    assert saved.reserved_quantity == first.quantity - filled
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
    assert completed[-1].submitted_orders == 0
    assert all(record in ledger.history() for record in original_history)
