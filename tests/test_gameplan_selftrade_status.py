"""Gameplan deferral is normal; missing evidence and genuine failures stay visible."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader import independent_session as worker
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.session_reporting import format_session_cycle_report, session_cycle_report
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
from test_account_gameplan_execution import account_env, native, run as account_run, state as account_state
from test_catchup_rejection_guard import working_order
from test_gameplan_direction_runtime import prepare, run as native_run
from test_independent_stock_runtime import ACCOUNT, _decisions, environment


@pytest.fixture(params=["native", "account"])
def conflict_env(request, monkeypatch):
    mode = request.param
    env = request.getfixturevalue("account_env" if mode == "account" else "environment")
    if mode == "native":
        prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1.
    current = {"orders": (working_order(),)}
    if mode == "account":
        env.portfolio_transform = lambda p: replace(p, broker_working_orders=current["orders"],
                                                    working_order_count=len(current["orders"]))
    else:
        original = runtime.capture_portfolio_state
        monkeypatch.setattr(runtime, "capture_portfolio_state", lambda *a, **kw:
                            replace(original(*a, **kw), broker_working_orders=current["orders"],
                                    working_order_count=len(current["orders"])))
    return env, account_run if mode == "account" else native_run, current, mode


def _reservations(env):
    return HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations


def _report(cycle, *, failed=0, consecutive=0):
    return session_cycle_report(cycle, as_of="2026-09-08T11:01:00Z", session_cycles=2,
        session_orders_submitted=cycle.get("submitted_orders", 0), session_failed_cycles=failed,
        session_consecutive_failures=consecutive)


def test_actual_conflict_is_not_a_runtime_error(conflict_env):
    env, run, _, _ = conflict_env
    result = run(env)
    assert result.status == "SELF_TRADE_CONFLICT_DEFERRED"
    assert result.broker_state_capture["status"] == "CURRENT"
    assert result.error is None
    assert result.stopped_after_error is False
    report = _report(result.to_dict())
    assert report["activity"] == "DEFERRED"
    assert "opposing" in report["message"].lower() and "defer" in report["message"].lower()


def test_repeated_conflict_preserves_intention_and_resumes_only_after_observed_clear(conflict_env):
    env, run, current, mode = conflict_env
    signal = deepcopy(vars(env.signals["AAPL", "1h"]))
    decisions = []
    for _ in range(2):
        result = run(env)
        assert result.status == "SELF_TRADE_CONFLICT_DEFERRED" and result.submitted_orders == 0
        recorded = _decisions(result)
        decision, = [d for d in recorded["decisions"] if d["symbol"] == "AAPL"]
        decisions.append(decision)
        assert decision["quantity"] == 0 and decision["order_payload"] is None
        assert decision["decision_reason_code"] == "OPPOSING_SAME_PRICE_WORKING_ORDER"
        deferred, = recorded["prediction_handoff"]["self_trade_prevention"]["deferred"]
        assert deferred["conflicts"][0]["order_id"] == current["orders"][0]["order_id"]
        assert _reservations(env) == () and env.broker.submissions == env.broker.cancellations == []
        if mode == "account":
            assert account_state(env)["reservations"] == []
        env.now += pd.Timedelta(seconds=1)
    assert decisions[0]["decision_id"] == decisions[1]["decision_id"]
    current["orders"] = ()
    env.pending_sell.clear()
    resumed = run(env)
    assert resumed.status == "ORDERS_SUBMITTED" and resumed.submitted_orders == 1, resumed.error
    decision, = [d for d in _decisions(resumed)["decisions"] if d["symbol"] == "AAPL"]
    assert decision["decision_id"] == decisions[0]["decision_id"]
    assert decision["prediction"]["prediction_id"] == signal["prediction_id"]
    assert vars(env.signals["AAPL", "1h"]) == signal
    assert len(env.broker.submissions) == 1 and env.broker.cancellations == []
    env.now += pd.Timedelta(seconds=1)
    assert run(env).submitted_orders == 0  # Existing reservation prevents replay.
    assert len(env.broker.submissions) == 1 and env.broker.cancellations == []


def test_unaffected_account_order_and_deferred_intention_are_both_reported(account_env):
    env = account_env
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1.
    env.portfolio_transform = lambda p: replace(p, broker_working_orders=(working_order(),), working_order_count=1)
    result = account_run(env)
    assert result.status == "ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT" and result.submitted_orders == 1
    assert result.error is None
    assert [p["orderLegCollection"][0]["instrument"]["symbol"] for p in env.broker.submissions] == ["NVDA"]
    assert {r.symbol for r in native(env).snapshot().reservations} == {"NVDA"}
    assert env.broker.cancellations == []
    report = _report(result.to_dict())
    assert report["activity"] == "ORDERS SUBMITTED"
    assert "opposing" in report["message"].lower() and "defer" in report["message"].lower()
    assert "fills are not confirmed" in report["message"].lower()


def test_unaffected_native_order_and_deferred_intention_are_both_reported(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {key: env.signals[key] for key in (("AAPL", "1h"), ("NVDA", "4h"))}
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1.
    original = runtime.capture_portfolio_state
    monkeypatch.setattr(runtime, "capture_portfolio_state", lambda *a, **kw:
                        replace(original(*a, **kw), broker_working_orders=(working_order(),), working_order_count=1))
    result = native_run(env)
    assert result.status == "ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT" and result.submitted_orders == 1
    assert result.error is None
    assert [p["orderLegCollection"][0]["instrument"]["symbol"] for p in env.broker.submissions] == ["NVDA"]
    assert {r.symbol for r in _reservations(env)} == {"NVDA"} and env.broker.cancellations == []
    report = _report(result.to_dict())
    assert report["activity"] == "ORDERS SUBMITTED"
    assert "opposing" in report["message"].lower() and "defer" in report["message"].lower()
    assert "fills are not confirmed" in report["message"].lower()


def test_missing_current_order_evidence_is_still_an_error(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original = runtime.capture_portfolio_state
    monkeypatch.setattr(runtime, "capture_portfolio_state", lambda *a, **kw:
                        replace(original(*a, **kw), broker_working_orders=None, working_order_count=1))
    result = native_run(env)
    assert result.status == "WORKING_ORDER_EVIDENCE_UNAVAILABLE" and result.error
    assert result.submitted_orders == 0 and env.broker.submissions == env.broker.cancellations == []
    assert _reservations(env) == ()
    assert _report(result.to_dict())["activity"] == "ATTENTION"


def test_true_unavailable_entry_quote_wins_over_another_symbols_normal_deferral(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {key: env.signals[key] for key in (("AAPL", "1h"), ("NVDA", "4h"))}
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1.
    original = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        portfolio = original(*args, **kwargs)
        return replace(portfolio, broker_working_orders=(working_order(),), working_order_count=1,
                       quotes={symbol: quote for symbol, quote in portfolio.quotes.items() if symbol != "NVDA"})
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
        broker_state_retry_max_seconds=0, broker_state_retry_sleep=lambda _: pytest.fail("No real wait"))
    assert result.status == "HORIZON_ENTRY_QUOTE_UNAVAILABLE" and "NVDA" in result.error
    assert result.broker_state_capture["status"] == "CURRENT"
    assert result.submitted_orders == 0 and env.broker.submissions == env.broker.cancellations == []
    assert _reservations(env) == ()
    recorded = _decisions(result)
    deferred, = recorded["prediction_handoff"]["self_trade_prevention"]["deferred"]
    assert deferred["symbol"] == "AAPL" and deferred["reason_code"] == "OPPOSING_SAME_PRICE_WORKING_ORDER"
    assert {d["symbol"]: d["decision_reason_code"] for d in recorded["decisions"]} == {
        "AAPL": "OPPOSING_SAME_PRICE_WORKING_ORDER", "NVDA": "USABLE_QUOTE_UNAVAILABLE"}
    assert _report(result.to_dict())["activity"] == "ATTENTION"


@pytest.mark.parametrize("status,submitted", [
    ("SELF_TRADE_CONFLICT_DEFERRED", 0), ("ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT", 2),
])
def test_exact_normal_conflict_statuses_explain_deferral_without_rewriting_cycle(status, submitted):
    cycle = {"status": status, "submitted_orders": submitted, "error": None,
             "stopped_after_error": False, "broker_state_capture": {"status": "CURRENT"}}
    original = deepcopy(cycle)
    report = _report(cycle)
    assert report["activity"] == ("ORDERS SUBMITTED" if submitted else "DEFERRED")
    line = format_session_cycle_report(report)
    assert "opposing" in line.lower() and "defer" in line.lower() and "ATTENTION" not in line
    assert f"submitted cycle={submitted}" in line
    assert cycle == original


@pytest.mark.parametrize("status", ["SELF_TRADE_CONFLICT_DEFERRED", "ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT"])
@pytest.mark.parametrize("failure", [{"error": "retained real failure"}, {"stopped_after_error": True},
                                     {"consecutive": 1}])
def test_normal_status_cannot_hide_real_failure_or_prior_unresolved_failure(status, failure):
    cycle = {"status": status, "submitted_orders": 1, "broker_state_capture": {"status": "CURRENT"}}
    cycle.update({k: v for k, v in failure.items() if k != "consecutive"})
    report = _report(cycle, failed=1, consecutive=failure.get("consecutive", 0))
    assert report["activity"] == "ATTENTION"
    assert status in format_session_cycle_report(report)


@pytest.mark.parametrize("status", ["FUTURE_SELF_TRADE_CONFLICT", "SELF_TRADE_CONFLICT_DEFERRED_FAILED",
                                    "WORKING_ORDER_EVIDENCE_UNAVAILABLE"])
def test_unknown_conflict_and_unavailable_statuses_keep_attention(status):
    assert _report({"status": status})["activity"] == "ATTENTION"


class Clock:
    def __init__(self):
        self.now = pd.Timestamp("2026-09-08T11:01:00Z")

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += pd.Timedelta(seconds=seconds)


def _run_session(tmp_path, monkeypatch, payloads):
    clock = Clock()
    attempts, saved, reports = [], [], []
    path = tmp_path / "state/independent-stock-trader/session-status.json"
    monkeypatch.setattr(worker, "_has_inventory", lambda path: True)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent",
                        lambda root: SimpleNamespace(active=len(attempts) < len(payloads)))
    def runner(root, **kwargs):
        assert root == tmp_path.resolve() and kwargs["execute"] is False
        assert kwargs["session_managed"] is True and kwargs["sizing_policy"] == GAMEPLAN_SIZING_POLICY
        payload = payloads[len(attempts)]
        attempts.append(clock())
        return SimpleNamespace(submitted_orders=payload.get("submitted_orders", 0), to_dict=lambda: deepcopy(payload))
    def sleep(seconds):
        saved.append(json.loads(path.read_text()))
        clock.sleep(seconds)
    worker.run_independent_stock_session(tmp_path, execute=False, runner=runner, clock=clock,
        sleep=sleep, reporter=reports.append, sizing_policy=GAMEPLAN_SIZING_POLICY)
    assert len(attempts) == len(payloads) and len(saved) == len(payloads)
    assert [s["last_cycle"] for s in saved] == payloads
    return saved, reports[1:]


@pytest.mark.parametrize("capture", [None, "CURRENT", "CURRENT_AFTER_RETRY"])
def test_actual_normal_deferral_does_not_inflate_failure_counters(conflict_env, tmp_path, monkeypatch, capture):
    env, run, _, _ = conflict_env
    payload = run(env).to_dict()
    payload["broker_state_capture"] = None if capture is None else {"status": capture}
    saved, reports = _run_session(tmp_path / "session", monkeypatch, [payload])
    assert saved[0]["failed_cycles"] == saved[0]["consecutive_failures"] == 0
    assert saved[0]["status"] == "RUNNING" and "ATTENTION" not in reports[0]
    assert env.broker.submissions == env.broker.cancellations == []


@pytest.mark.parametrize("capture", [None, "CURRENT", "CURRENT_AFTER_RETRY"])
def test_deferral_clears_prior_failure_only_after_current_capture(conflict_env, tmp_path, monkeypatch, capture):
    env, run, _, _ = conflict_env
    payload = run(env).to_dict()
    payload["broker_state_capture"] = None if capture is None else {"status": capture}
    first = {"status": "WORKING_ORDER_EVIDENCE_UNAVAILABLE", "error": "missing current evidence",
             "submitted_orders": 0, "broker_state_capture": {"status": "CURRENT"}}
    saved, reports = _run_session(tmp_path / "session", monkeypatch, [first, payload])
    assert [s["failed_cycles"] for s in saved] == [1, 1]
    assert [s["consecutive_failures"] for s in saved] == [1, int(capture is None)]
    assert saved[-1]["status"] == ("DEGRADED" if capture is None else "RUNNING")
    assert ("ATTENTION" in reports[-1]) == (capture is None)
    assert env.broker.submissions == env.broker.cancellations == []


@pytest.mark.parametrize("failure", [
    {"status": "WORKING_ORDER_EVIDENCE_UNAVAILABLE", "error": "retained unavailable evidence"},
    {"status": "SELF_TRADE_CONFLICT_DEFERRED", "error": "retained unrelated exception"},
    {"status": "SELF_TRADE_CONFLICT_DEFERRED", "stopped_after_error": True},
    {"status": "SUBMISSION_STOPPED_AFTER_ERROR", "error": "retained submission exception"},
])
def test_session_keeps_genuine_errors_failed(tmp_path, monkeypatch, failure):
    payload = {**failure, "submitted_orders": 0, "broker_state_capture": {"status": "CURRENT"}}
    saved, reports = _run_session(tmp_path, monkeypatch, [payload])
    assert saved[0]["failed_cycles"] == saved[0]["consecutive_failures"] == 1
    assert saved[0]["status"] == "DEGRADED" and "ATTENTION" in reports[0]
