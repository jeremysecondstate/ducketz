"""Independent runtime integration with synthetic broker evidence only."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.contracts import ActivationIntent, EnrichmentOutput, PortfolioState, PredictionSignal, QuoteState
from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, OrderEvidence, PortfolioEvidence


ACCOUNT = "a" * 64
BROKER_ID = "b" * 64
SYMBOLS = ("AAPL", "AMZN", "COST", "GOOG", "MU", "NVDA", "SNDK")
NOW = pd.Timestamp("2026-09-08T11:01:00Z")


def test_inventory_poll_releases_database_file_without_garbage_collection(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE allocations (status TEXT)")
    connection.close()
    for _ in range(20):
        assert runtime._has_inventory(path) is False
    # Windows rejects unlink while any polling connection still owns the file.
    path.unlink()
    assert not path.exists()


class Model:
    model_fingerprint = "fixture-hourly-enrichment"
    supported_horizons = ("1h",)
    qualified_target_contracts = ()

    def predict(self, features):
        return EnrichmentOutput("fixture", "v1", self.model_fingerprint, .9, 1., .02, .01, .9, 0., .03, 60.)


class Broker:
    def __init__(self):
        self.calls = []
        self.submissions = []
        self.cancellations = []
        self.prepared_identity = BROKER_ID
        self.unknown_submission = False
        self.before_post_hook = None

    def stable_account_fingerprint(self):
        self.calls.append("stable_identity")
        return ACCOUNT

    def verify_read_snapshot(self, expected):
        self.calls.append("verify_snapshot")
        assert expected == BROKER_ID

    def prepare_order_submission(self):
        self.calls.append("prepare")
        return SimpleNamespace(identity_fingerprint=self.prepared_identity)

    def submit_prepared_order(self, payload, context, *, before_post):
        if self.before_post_hook:
            self.before_post_hook()
        before_post()
        self.calls.append("post")
        self.submissions.append(payload)
        if self.unknown_submission:
            raise TimeoutError("synthetic connection dropped after possible acceptance")
        return f"https://api.schwabapi.com/trader/v1/accounts/fixture/orders/{1000 + len(self.submissions)}"

    def cancel_prepared_order(self, order_id, context, *, before_delete):
        before_delete()
        self.calls.append("delete")
        self.cancellations.append(order_id)


def _signals():
    result = {}
    for symbol in SYMBOLS:
        for horizon, hours in (("1h", 1), ("4h", 4), ("1d", 13), ("1w", 133)):
            result[symbol, horizon] = PredictionSignal(
                symbol, horizon, f"{symbol}:{horizon}:fixture", "2026-09-05T00:00:00Z",
                "2026-09-08T11:00:00Z", (NOW.floor("h") + pd.Timedelta(hours=hours)).isoformat(),
                "2026-09-08T11:05:00Z", "2026-09-08T06:00:00Z", .7, .001,
                {horizon: .7}, "fixture", "v1", "source", "PRE", "independent-stock-targets-v1",
            )
    return result


@pytest.fixture
def environment(tmp_path, monkeypatch):
    env = SimpleNamespace(root=tmp_path, now=NOW, active=True, signals=_signals(), model=Model(),
                          held={symbol: 0. for symbol in SYMBOLS}, pending_sell={}, broker=Broker(), captures=0,
                          after_capture=None)
    monkeypatch.setattr(runtime, "read_gameplan_stock_activation_intent", lambda root: ActivationIntent(
        env.active, "ACTIVE" if env.active else "INACTIVE", "fixture", "fixture", "fixture-switch"
    ))
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", lambda root, **kwargs: (env.signals, ()))
    monkeypatch.setattr(runtime, "load_current_enrichment_model", lambda root: env.model)
    monkeypatch.setattr(runtime, "SchwabSession", lambda: pytest.fail("A real broker session must never be constructed in tests"))
    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence", lambda broker, ledger, **kwargs: ())

    def capture(broker, *, observed_at, parallel):
        env.captures += 1
        broker.calls.append("capture")
        result = PortfolioState(
            env.now.isoformat(), 100_000., 100_000., sum(env.held.values()) * 100, 0., dict(env.held),
            {symbol: env.held[symbol] * 100 for symbol in SYMBOLS}, {}, dict(env.pending_sell), 0,
            {symbol: QuoteState(symbol, 100., 100., 100., 100., 1000., env.now.isoformat()) for symbol in SYMBOLS},
            "portfolio-" + env.now.isoformat(), BROKER_ID,
        )
        if env.after_capture:
            env.after_capture()
        return result

    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    return env


def _qualify(monkeypatch):
    # Only execution plumbing and shared budgets use synthetic qualification.
    # The real hourly-only qualification rejection is tested separately below.
    monkeypatch.setattr(runtime, "enrichment_signal_readiness", lambda model, signal: {"status": "READY"})
    monkeypatch.setattr("ml.stock_trader.model.require_enrichment_signal_support", lambda model, signal: None)


def _run(env, *, execute=True, entries=True, session_managed=True):
    return runtime.run_independent_stock_trader_once(
        env.root, execute=execute, session=env.broker, runtime_clock=lambda: env.now, entries=entries,
        session_managed=session_managed,
    )


def _decisions(result):
    return json.loads((result.run_directory / "decisions.json").read_text(encoding="utf-8"))


def _retry_run(env, *, budget=120., on_sleep=None):
    elapsed = [0.]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
        env.now += pd.Timedelta(seconds=seconds)
        if on_sleep:
            on_sleep()
    result = runtime.run_independent_stock_trader_once(
        env.root, execute=True, session=env.broker, runtime_clock=lambda: env.now,
        session_managed=True, sizing_policy="fixed-horizon-budget-v1",
        broker_state_retry_max_seconds=budget, broker_state_retry_sleep=sleep,
        broker_state_retry_clock=lambda: elapsed[0],
    )
    return result, sleeps


def test_independent_read_timeout_recaptures_history_and_complete_portfolio(environment, monkeypatch):
    env = environment
    env.signals = _fixed_signals()
    original = runtime.capture_portfolio_state
    history_reads, attempts = [], []
    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence",
                        lambda *args, **kwargs: history_reads.append(env.now) or ())
    def flaky(*args, **kwargs):
        attempts.append(env.now)
        if len(attempts) < 3:
            error = requests.ReadTimeout("synthetic broker read timeout")
            error.stock_trader_operation = "open_orders"
            raise error
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, "capture_portfolio_state", flaky)
    result, sleeps = _retry_run(env)
    assert result.status == "ORDERS_SUBMITTED"
    assert len(history_reads) == len(attempts) == 3
    assert sleeps == [3., 3.]
    assert result.submitted_orders == len(env.broker.submissions) == 6
    assert result.broker_state_capture["status"] == "CURRENT_AFTER_RETRY"
    assert result.broker_state_capture["last_error_operation"] == "open_orders"
    assert _decisions(result)["broker_state_capture"] == result.broker_state_capture
    receipt = json.loads((result.run_directory / "receipt.json").read_text())
    assert receipt["broker_state_capture"] == result.broker_state_capture


@pytest.mark.parametrize("failure", ["auth", "uncertain_auth", "timeout"])
def test_independent_broker_read_failure_obeys_classification_and_budget(environment, monkeypatch, failure):
    env = environment
    env.signals = _fixed_signals()
    attempts = []
    def failed(*args, **kwargs):
        attempts.append(env.now)
        error = requests.ReadTimeout("synthetic timeout")
        if failure == "auth":
            response = requests.Response()
            response.status_code = 401
            error = requests.HTTPError("401 unauthorized", response=response)
        elif failure == "uncertain_auth":
            error.schwab_retry_safe = False
        error.stock_trader_operation = "session_preflight"
        raise error
    monkeypatch.setattr(runtime, "capture_portfolio_state", failed)
    result, sleeps = _retry_run(env, budget=7.)
    expected_attempts = 3 if failure == "timeout" else 1
    assert len(attempts) == expected_attempts
    assert sleeps == ([3., 3.] if failure == "timeout" else [])
    assert result.status == "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE"
    assert result.broker_state_capture["attempts"] == expected_attempts
    assert result.submitted_orders == 0
    assert env.broker.submissions == env.broker.cancellations == []
    assert _decisions(result)["broker_state_capture"] == result.broker_state_capture


def test_independent_retry_stops_when_operator_disables_worker(environment, monkeypatch):
    env = environment
    env.signals = _fixed_signals()
    attempts = []
    def failed(*args, **kwargs):
        attempts.append(env.now)
        raise requests.ReadTimeout("synthetic broker timeout")
    monkeypatch.setattr(runtime, "capture_portfolio_state", failed)
    result, sleeps = _retry_run(env, on_sleep=lambda: setattr(env, "active", False))
    assert len(attempts) == 1 and sleeps == [3.]
    assert "TRADER_INACTIVE_DURING_BROKER_CAPTURE" in result.error
    assert env.broker.submissions == env.broker.cancellations == []


def test_independent_retry_never_replays_expired_entry_or_failed_slot(environment, monkeypatch):
    env = environment
    env.signals = _fixed_signals()
    original = runtime.capture_portfolio_state
    attempts = []
    def failed(*args, **kwargs):
        attempts.append(env.now)
        raise requests.ReadTimeout("synthetic broker timeout")
    monkeypatch.setattr(runtime, "capture_portfolio_state", failed)
    result, sleeps = _retry_run(env, on_sleep=lambda: setattr(env, "now", NOW + pd.Timedelta(minutes=5)))
    assert len(attempts) == 1 and sleeps == [3.]
    assert "BROKER_READ_EXECUTION_WINDOW_CLOSED" in result.error
    monkeypatch.setattr(runtime, "capture_portfolio_state", original)
    rerun, _ = _retry_run(env)
    assert result.submitted_orders == rerun.submitted_orders == 0
    assert env.broker.submissions == env.broker.cancellations == []


def test_unpromoted_forecast_set_exits_without_any_broker_read(environment):
    env = environment
    env.signals = {}  # The verified loader removes every unpromoted forecast.
    result = _run(env)
    assert result.status == "NO_QUALIFIED_INDEPENDENT_STOCK_ENTRIES"
    assert result.submitted_orders == 0
    assert env.broker.calls == []
    assert env.captures == 0
    assert not (env.root / runtime.LEDGER_RELATIVE_PATH).exists()


def _fixed_signals():
    from datetime import date
    from ml.independent_stock_targets import stock_target_windows
    windows = {spec["model_group"]: spec for spec in stock_target_windows(date(2026, 9, 8))
               if spec["execution_eligible"] and pd.Timestamp(spec["target_window_start"]) == NOW.floor("h")}
    return {key: replace(signal, target_window_end=windows[key[1]]["target_window_end"].isoformat(),
                         target_price_source_contract="xnas-itch-archive-v1")
            for key, signal in _signals().items()}


def test_explicit_fixed_budget_mode_uses_verified_forecasts_and_shared_submission_path(environment, monkeypatch):
    env = environment
    env.signals = _fixed_signals()
    checked = []
    def load(root, **kwargs):
        checked.append(kwargs["require_promoted_model_reports"])
        return env.signals, ()
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", load)
    monkeypatch.setattr(runtime, "load_current_enrichment_model", lambda root: pytest.fail("Fixed sizing must not relabel a learned model"))
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy="fixed-horizon-budget-v1")
    assert checked == [True]
    assert result.submitted_orders == 6
    assert len(env.broker.submissions) == 6
    decisions = _decisions(result)["decisions"]
    assert all(d["expected_net_return"] is None and d["trade_probability"] is None for d in decisions)
    assert all(order["orderType"] == "LIMIT" for order in env.broker.submissions)


def test_fixed_budget_bearish_forecasts_are_no_entry_not_missing_model(environment, monkeypatch):
    env = environment
    env.signals = {key: replace(signal, calibrated_probability=.4) for key, signal in _fixed_signals().items()}
    monkeypatch.setattr(runtime, "load_current_enrichment_model", lambda root: pytest.fail("Research sizing is not the selected strategy"))
    result = runtime.run_independent_stock_trader_once(env.root, execute=False, session=env.broker,
        runtime_clock=lambda: env.now, sizing_policy="fixed-horizon-budget-v1")
    assert result.status == "DRY_RUN_INDEPENDENT_STOCK_DECISIONS"
    assert result.submitted_orders == result.selected_orders == 0
    assert env.broker.submissions == []
    assert {row["decision_reason_code"] for row in _decisions(result)["decisions"]} == {"NO_BULLISH_ENTRY_SIGNAL"}


def test_fixed_policy_still_stops_before_post_if_stock_switch_changes(environment):
    env = environment
    env.signals = _fixed_signals()
    env.broker.before_post_hook = lambda: setattr(env, "active", False)
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy="fixed-horizon-budget-v1")
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert env.broker.submissions == []


def test_real_hourly_enrichment_rejects_all_independent_signals_before_broker_read(environment):
    env = environment
    result = _run(env)
    assert result.status == "NO_QUALIFIED_INDEPENDENT_STOCK_ENTRIES"
    metadata = _decisions(result)["prediction_handoff"]
    assert metadata["directional_signals"] == 28
    assert metadata["qualified_entry_signals"] == 0
    assert set(item["reason"] for item in metadata["enrichment_readiness"].values()) == {"ENRICHMENT_INDEPENDENT_TARGET_CONTRACT_NOT_QUALIFIED"}
    assert env.broker.calls == []
    assert env.captures == 0


@pytest.mark.parametrize(("active", "clock", "expected"), [
    (False, NOW, "TRADER_INACTIVE"),
    (True, pd.Timestamp("2026-09-09T00:00:00Z"), "EXECUTION_WINDOW_CLOSED"),
])
def test_inactive_and_closed_session_do_not_touch_broker(environment, active, clock, expected):
    env = environment
    env.active, env.now = active, clock
    result = _run(env)
    assert result.status == expected
    assert env.broker.calls == []


def test_combined_all_horizon_slot_has_six_order_cap_and_acceptance_does_not_create_shares(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    result = _run(env)
    assert result.status == "ORDERS_SUBMITTED"
    assert result.submitted_orders == len(env.broker.submissions) == 6
    selected = [decision for decision in _decisions(result)["decisions"] if decision["quantity"] > 0]
    assert len(selected) == 6
    assert len({decision["decision_id"] for decision in selected}) == 6
    assert sum(decision["quantity"] * decision["limit_price"] for decision in selected) <= 95_000
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    snapshot = ledger.snapshot()
    assert len(snapshot.reservations) == 6
    assert all(item.status == "SUBMITTED" and item.filled_quantity == 0 for item in snapshot.reservations)
    assert all(item.filled_shares == 0 for item in snapshot.allocations)
    assert len(list((env.root / "state/independent-stock-trader/entry-slots").glob("*.json"))) == 1
    env.now += pd.Timedelta(seconds=1)
    second = _run(env)
    assert second.submitted_orders == 0
    assert len(env.broker.submissions) == 6
    assert _decisions(second)["prediction_handoff"]["entry_slot_already_consumed"] is True


def test_dry_run_does_not_consume_entry_slot_or_submit(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    result = _run(env, execute=False, session_managed=False)
    assert result.status == "DRY_RUN_INDEPENDENT_STOCK_DECISIONS"
    assert result.submitted_orders == 0
    assert env.broker.submissions == []
    assert not (env.root / "state/independent-stock-trader/entry-slots").exists()
    assert HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations == ()


def test_control_disabled_after_capture_or_during_final_gate_prevents_post(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    env.after_capture = lambda: setattr(env, "active", False)
    result = _run(env)
    assert result.status == "TRADER_INACTIVE_AFTER_BROKER_CAPTURE"
    assert env.broker.submissions == []


def test_prepared_identity_change_is_rejected_before_reservation(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    env.broker.prepared_identity = "c" * 64
    result = _run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert "BROKER_ACCOUNT_IDENTITY_CHANGED_BEFORE_SUBMISSION" in result.error
    assert env.broker.submissions == []
    assert HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations == ()


def test_final_control_change_rejects_reservation_and_never_posts(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    env.broker.before_post_hook = lambda: setattr(env, "active", False)
    result = _run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert env.broker.submissions == []
    reservations = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations
    assert len(reservations) == 1
    assert reservations[0].status == "REJECTED"


def test_uncertain_submission_stays_reserved_and_is_never_retried(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    env.broker.unknown_submission = True
    result = _run(env)
    assert result.status == "SUBMISSION_STOPPED_AFTER_ERROR"
    assert len(env.broker.submissions) == 1
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    reservation = ledger.snapshot().reservations[0]
    assert reservation.status == "UNKNOWN"
    assert reservation.reserved_quantity > 0
    assert reservation.filled_quantity == 0
    env.now += pd.Timedelta(seconds=1)
    rerun = _run(env)
    assert rerun.submitted_orders == 0
    assert len(env.broker.submissions) == 1
    assert ledger.snapshot().reservations[0].status == "UNKNOWN"


def _owned_allocation(env, *, symbol="COST", horizon="1h", quantity=10, manual=90, filled_quantity=None):
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    seeded_at = "2026-09-08T11:00:00+00:00"
    initial = PortfolioEvidence("seed", ACCOUNT, seeded_at, {symbol: manual}, {symbol: 100.}, {symbol: 15_000.}, "seed-source")
    assert ledger.reconcile(initial).ready
    reservation = ledger.reserve_entry(
        symbol=symbol, horizon=horizon, forecast_id="owned-original-forecast", target_start=seeded_at,
        target_end="2026-09-08T12:00:00+00:00", quantity=quantity, limit_price=100., snapshot_id="seed",
        idempotency_key="owned-entry", batch_id="owned-batch", as_of=seeded_at,
    )
    filled_at = "2026-09-08T11:00:20+00:00"
    filled_quantity = quantity if filled_quantity is None else filled_quantity
    status = "FILLED" if filled_quantity == quantity else "PARTIAL"
    evidence = OrderEvidence(
        "filled-evidence", reservation.reservation_id, ACCOUNT, filled_at, "12345", status,
        quantity, filled_quantity, quantity - filled_quantity,
        (FillEvidence("owned-fill", filled_quantity, 100., filled_at),),
    )
    filled = replace(initial, snapshot_id="seed-filled", observed_at="2026-09-08T11:00:21+00:00", held_shares={symbol: manual + filled_quantity})
    assert ledger.reconcile(filled, order_evidence=(evidence,)).ready
    env.held[symbol] = manual + filled_quantity
    return ledger


def test_mature_exit_works_without_current_plan_or_model_and_sells_only_owned_shares(environment, monkeypatch):
    env = environment
    ledger = _owned_allocation(env)
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")

    def unavailable(*args, **kwargs):
        raise ValueError("Missing or incompatible current publication")

    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", unavailable)
    monkeypatch.setattr(runtime, "load_current_enrichment_model", unavailable)
    result = _run(env, session_managed=False)
    assert result.status == "ORDERS_SUBMITTED"
    assert result.submitted_orders == 1
    assert env.broker.submissions[0]["orderLegCollection"][0]["quantity"] == 10
    assert env.broker.submissions[0]["orderLegCollection"][0]["instruction"] == "SELL"
    snapshot = ledger.snapshot()
    assert snapshot.allocations[0].filled_shares == 10
    assert snapshot.allocations[0].reserved_sell_shares == 10
    selected = [decision for decision in _decisions(result)["decisions"] if decision["quantity"]]
    assert selected[0]["prediction"]["holding_target_end"] == "2026-09-08T12:00:00+00:00"


def test_exit_respects_other_working_sell_reservations_in_portfolio(environment):
    env = environment
    _owned_allocation(env, quantity=10, manual=0)
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")
    env.signals = {}
    env.pending_sell = {"COST": 7}
    result = _run(env)
    assert result.submitted_orders == 1
    assert env.broker.submissions[0]["orderLegCollection"][0]["quantity"] == 3


def test_two_due_horizon_exits_share_one_available_sell_budget(environment):
    env = environment
    ledger = _owned_allocation(env, quantity=10, manual=0)
    second = ledger.reserve_entry(
        symbol="COST", horizon="4h", forecast_id="second-owned-forecast",
        target_start="2026-09-08T08:00:00+00:00", target_end="2026-09-08T12:00:00+00:00",
        quantity=10, limit_price=100., snapshot_id="seed-filled", idempotency_key="second-owned-entry",
        batch_id="second-owned-batch", as_of="2026-09-08T11:00:22+00:00",
    )
    filled_at = "2026-09-08T11:00:23+00:00"
    evidence = OrderEvidence(
        "second-filled-evidence", second.reservation_id, ACCOUNT, filled_at, "12346", "FILLED", 10, 10, 0,
        (FillEvidence("second-owned-fill", 10, 100., filled_at),),
    )
    assert ledger.reconcile(PortfolioEvidence(
        "second-filled-snapshot", ACCOUNT, "2026-09-08T11:00:24+00:00", {"COST": 20},
        {"COST": 100.}, {"COST": 15_000.}, "second-fill-source",
    ), order_evidence=(evidence,)).ready
    env.held["COST"] = 20
    env.pending_sell = {"COST": 8}
    env.signals = {}
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")
    result = _run(env)
    assert result.submitted_orders == 2
    quantities = [payload["orderLegCollection"][0]["quantity"] for payload in env.broker.submissions]
    assert sorted(quantities) == [2, 10]
    assert sum(quantities) == 12
    assert sum(allocation.reserved_sell_shares for allocation in ledger.snapshot().allocations) == 12


def test_closed_control_blocks_even_mature_owned_exit(environment):
    env = environment
    _owned_allocation(env)
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")
    env.active = False
    result = _run(env)
    assert result.status == "TRADER_INACTIVE"
    assert env.broker.calls == []


def test_expired_partial_entry_is_cancelled_once_and_exits_wait_for_terminal_evidence(environment, monkeypatch):
    env = environment
    ledger = _owned_allocation(env, quantity=10, manual=0, filled_quantity=5)
    reservation = ledger.snapshot().reservations[0]
    env.signals = {}
    env.now = pd.Timestamp("2026-09-08T11:05:00Z")
    status = ["PARTIAL"]

    def order_evidence(*args, **kwargs):
        return (OrderEvidence(
            "fresh-entry:" + env.now.isoformat(), reservation.reservation_id, ACCOUNT,
            env.now.isoformat(), "12345", status[0], 10, 5, 5 if status[0] == "PARTIAL" else 0,
            (FillEvidence("owned-fill", 5, 100., "2026-09-08T11:00:20+00:00"),),
        ),)

    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence", order_evidence)
    first = _run(env)
    assert first.status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION"
    assert env.broker.cancellations == ["12345"]
    assert env.broker.submissions == []
    assert ledger.snapshot().allocations[0].filled_shares == 5
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")
    second = _run(env)
    assert second.submitted_orders == 0
    assert env.broker.cancellations == ["12345"]
    assert env.broker.submissions == []
    blocked = _decisions(second)["decisions"]
    assert blocked and blocked[0]["decision_reason_code"] == "HORIZON_EXIT_BLOCKED"
    assert "ENTRY_ORDER_STILL_WORKING_OR_UNKNOWN" in blocked[0]["decision_reason"]
    status[0] = "CANCELLED"
    env.now += pd.Timedelta(seconds=1)
    third = _run(env)
    assert third.submitted_orders == 1
    assert env.broker.submissions[0]["orderLegCollection"][0]["quantity"] == 5
    assert env.broker.cancellations == ["12345"]


def test_plain_live_once_cannot_open_entries_without_session_manager(environment, monkeypatch):
    env = environment
    _qualify(monkeypatch)
    result = _run(env, session_managed=False)
    assert result.status == "ENTRY_REQUIRES_SESSION_MANAGER"
    assert result.submitted_orders == 0
    assert env.broker.calls == []
    assert env.captures == 0
    assert not (env.root / runtime.LEDGER_RELATIVE_PATH).exists()
    assert not (env.root / "state/independent-stock-trader/entry-slots").exists()
    metadata = _decisions(result)["prediction_handoff"]
    assert metadata["session_managed"] is False
    assert metadata["entry_management_status"] == "ENTRY_REQUIRES_SESSION_MANAGER"
