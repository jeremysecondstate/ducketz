"""Offline native integration: real temporary ledgers, synthetic broker only."""
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, ACCOUNT, SYMBOLS, _fixed_signals
from ml.account_gameplan import authority, config, execution
from ml.account_gameplan.config import AccountConfig
from ml.stock_trader import gameplan_execution, independent_runtime as runtime
from ml.stock_trader.contracts import canonical_sha256, QuoteState
from ml.stock_trader.horizon_ledger import HorizonLedger, OrderEvidence, FillEvidence, PortfolioEvidence
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY, FIXED_SIZING_POLICY


@pytest.fixture
def account_env(environment, monkeypatch):
    env = environment
    env.config = AccountConfig("pc-original", "pc-original",
        {"pc-original": SYMBOLS[:4], "pc-new": SYMBOLS[4:]}, ACCOUNT, "e" * 64, {"status": "ACTIVE"})
    env.signals = {key: replace(value, source_fingerprint="combined-fixture")
                   for key, value in _fixed_signals().items() if key in {("AAPL", "1h"), ("NVDA", "4h")}}
    run = env.root / "ml/account-gameplan-runs/combined-fixture"
    run.mkdir(parents=True)
    (run / "receipt.json").write_text("{}")
    env.plan = SimpleNamespace(path=run.resolve(), manifest_sha256="f" * 64,
        report={"action_date": env.now.tz_convert("America/Los_Angeles").date().isoformat(),
                "sources": [{"producer_id": "pc-original", "source_receipt_sha256": "b" * 64},
                            {"producer_id": "pc-new", "source_receipt_sha256": "c" * 64}]},
        rows=pd.DataFrame([{"id": signal.prediction_id, "symbol": signal.symbol,
            "model_group": signal.primary_horizon, "calibrated_probability": signal.calibrated_probability,
            "target_window_start": signal.target_window_start, "target_window_end": signal.target_window_end,
            "producer_id": "pc-original" if signal.symbol in SYMBOLS[:4] else "pc-new"}
            for signal in env.signals.values()]))
    monkeypatch.setattr(config, "load_account_config", lambda root: env.config)
    monkeypatch.setattr(config, "verify_cutover", lambda root, cfg: None)
    monkeypatch.setattr(execution, "load_account_config", lambda root: env.config)
    monkeypatch.setattr(execution, "verify_cutover", lambda root, cfg: None)
    monkeypatch.setattr(gameplan_execution, "account_execution_plan", lambda root, **kwargs: (env.plan, env.config))
    capture = runtime.capture_portfolio_state
    env.portfolio_transform = lambda portfolio: portfolio
    env.order_evidence = ()
    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence",
                        lambda broker, ledger, **kwargs: env.order_evidence)
    def union_capture(broker, *, observed_at, parallel, **kwargs):
        assert kwargs == {"literal_cash_only": True, "use_actual_quote_timestamps": True,
                          "symbols": env.config.symbols, "include_order_identities": True}
        # Account observation follows complete order evidence; it is never
        # retimestamped later to disguise stale data.
        env.now += pd.Timedelta(milliseconds=1)
        result = capture(broker, observed_at=observed_at, parallel=parallel)
        return env.portfolio_transform(replace(result, source_fingerprint=canonical_sha256(result.source_fingerprint),
                                                broker_working_orders=()))
    monkeypatch.setattr(runtime, "capture_portfolio_state", union_capture)
    return env


def run(env, *, execute=True, policy=GAMEPLAN_SIZING_POLICY):
    return runtime.run_independent_stock_trader_once(env.root, execute=execute, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=policy,
        broker_state_retry_max_seconds=0)


def state(env):
    return authority.AccountAuthority(env.root / execution.AUTHORITY_PATH, execution._binding(env.config)).read_state()


def native(env):
    return HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)


def test_two_producers_use_one_account_authority_with_native_ids(account_env):
    env = account_env
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == len(env.broker.submissions) == 2
    records = state(env)["reservations"]
    assert {r["request"]["participant_id"] for r in records} == {"pc-original", "pc-new"}
    owned = {r.reservation_id: r for r in native(env).snapshot().reservations}
    for record in records:
        request = record["request"]
        reservation = owned[request["native_reservation_id"]]
        assert request["native_allocation_id"] == reservation.allocation_id
        assert record["broker_order_id"] == reservation.broker_order_id
        assert request["generation"] == env.plan.manifest_sha256
        assert record["status"] == reservation.status == "SUBMITTED"
    assert sum(Decimal(r["request"]["limit_price"]) * r["request"]["quantity"] for r in records) <= 95000


def test_explicit_22_symbol_union_uses_global_priority_and_one_cash_buffer(account_env):
    env = account_env
    symbols = tuple("XA" + chr(65 + i) for i in range(22))
    env.config = replace(env.config, participants={"pc-original": symbols[:11], "pc-new": symbols[11:]})
    template = next(iter(env.signals.values()))
    env.signals = {(symbol, "1h"): replace(template, symbol=symbol, primary_horizon="1h",
        prediction_id=symbol + "-forecast", calibrated_probability=.9 if symbol == symbols[-1] else .7)
        for symbol in symbols}
    env.plan.rows = pd.DataFrame([{"id": s.prediction_id, "symbol": s.symbol, "model_group": "1h",
        "calibrated_probability": s.calibrated_probability, "target_window_start": s.target_window_start,
        "target_window_end": s.target_window_end, "producer_id": "pc-original" if s.symbol in symbols[:11] else "pc-new"}
        for s in env.signals.values()])
    def union(p):
        return replace(p, available_cash=2000, held_shares=dict.fromkeys(symbols, 0.),
            symbol_exposure=dict.fromkeys(symbols, 0.),
            quotes={s: QuoteState(s, 100., 100., 100., 100., 1000., env.now.isoformat()) for s in symbols})
    env.portfolio_transform = union
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    records = state(env)["reservations"]
    assert result.submitted_orders == 2
    assert env.broker.submissions[0]["orderLegCollection"][0]["instrument"]["symbol"] == symbols[-1]
    assert sum(Decimal(r["request"]["limit_price"]) * r["request"]["quantity"] for r in records) == 1900


@pytest.mark.parametrize("change,policy,error", [
    ({"machine_id": "pc-new"}, GAMEPLAN_SIZING_POLICY, "FORECAST_PRODUCER"),
    ({}, FIXED_SIZING_POLICY, "REQUIRES_SELECTED_GAMEPLAN"),
    ({"activation": {"status": "PREPARING"}}, GAMEPLAN_SIZING_POLICY, "CUTOVER_NOT_ACTIVE"),
])
def test_unauthorized_mode_fails_before_any_broker_calls(account_env, change, policy, error):
    env = account_env
    env.config = replace(env.config, **change)
    with pytest.raises(ValueError, match=error):
        run(env, policy=policy)
    assert env.broker.calls == []
    assert not (env.root / execution.AUTHORITY_PATH).exists()


def test_account_mismatch_stops_before_portfolio_or_native_database(account_env):
    env = account_env
    env.config = replace(env.config, account_fingerprint="9" * 64)
    result = run(env)
    assert result.status == "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE"
    assert "BROKER_ACCOUNT_DIFFERS" in result.error
    assert env.broker.submissions == [] and env.captures == 0
    assert not (env.root / runtime.LEDGER_RELATIVE_PATH).exists()


def test_preview_does_not_create_account_authority_or_reservations(account_env):
    env = account_env
    assert run(env, execute=False).status == "DRY_RUN_INDEPENDENT_STOCK_DECISIONS"
    assert not (env.root / execution.AUTHORITY_PATH).exists()
    assert native(env).snapshot().reservations == ()


def test_subcent_ask_uses_conservative_cent_exposure(account_env):
    env = account_env
    env.portfolio_transform = lambda p: replace(p, quotes={s: replace(q, ask=100.0001) for s, q in p.quotes.items()})
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert all(r["request"]["exposure_price"] == "100.01" for r in state(env)["reservations"])


@pytest.mark.parametrize("working", [None, ({"status": "CURRENT", "asset_type": "OPTION", "instruction": "BUY"},)])
def test_unknown_or_unsupported_working_order_identity_blocks_all_submissions(account_env, working):
    env = account_env
    env.portfolio_transform = lambda p: replace(p, broker_working_orders=working)
    result = run(env)
    assert result.status == "ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE"
    assert env.broker.submissions == []
    assert native(env).snapshot().reservations == ()


def test_known_prepost_operator_stop_releases_both_unsubmitted_reservations(account_env):
    env = account_env
    env.broker.before_post_hook = lambda: setattr(env, "active", False)
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK", result.error
    assert env.broker.submissions == []
    assert [r.status for r in native(env).snapshot().reservations] == ["REJECTED"]
    assert [r["status"] for r in state(env)["reservations"]] == ["NOT_SUBMITTED"]


def test_unknown_post_retains_both_ledgers_and_never_repeats(account_env):
    env = account_env
    env.broker.unknown_submission = True
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_AFTER_ERROR"
    assert [r.status for r in native(env).snapshot().reservations] == ["UNKNOWN"]
    assert [r["status"] for r in state(env)["reservations"]] == ["UNKNOWN"]
    env.now += pd.Timedelta(seconds=1)
    assert run(env).submitted_orders == 0
    assert len(env.broker.submissions) == 1


def test_repeated_send_gate_keeps_both_native_and_account_reservations_unknown(account_env):
    env = account_env
    def ambiguous_adapter(payload, context, *, before_post):
        before_post()
        before_post()  # A malformed adapter must not cause a proven-not-sent release.
        pytest.fail("A repeated authority gate must stop")
    env.broker.submit_prepared_order = ambiguous_adapter
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_AFTER_ERROR"
    assert [r.status for r in native(env).snapshot().reservations] == ["UNKNOWN"]
    assert [r["status"] for r in state(env)["reservations"]] == ["UNKNOWN"]


def test_changed_source_in_prepost_gate_never_posts(account_env):
    env = account_env
    env.broker.before_post_hook = lambda: setattr(env, "plan", SimpleNamespace(
        **{**vars(env.plan), "manifest_sha256": "8" * 64}))
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert "PINNED_ACCOUNT_EXECUTION_PLAN_CHANGED" in result.error
    assert env.broker.submissions == []
    assert state(env)["reservations"][0]["status"] == "NOT_SUBMITTED"


def test_exact_partial_cancel_reconciles_both_ledgers_without_repeating_forecasts(account_env):
    env = account_env
    assert run(env).submitted_orders == 2
    env.now += pd.Timedelta(seconds=1)
    prior = native(env).snapshot().reservations
    env.order_evidence = tuple(OrderEvidence("terminal-" + r.reservation_id, r.reservation_id, ACCOUNT,
        env.now.isoformat(), r.broker_order_id, "CANCELLED", r.quantity, 1, 0,
        (FillEvidence("fill-" + r.reservation_id, 1, r.limit_price, env.now.isoformat()),)) for r in prior)
    for r in prior:
        env.held[r.symbol] = 1
    result = run(env)
    assert result.submitted_orders == 0, result.error
    assert result.status == "NO_ORDERS_SUBMITTED", result.error
    assert len(env.broker.submissions) == 2
    assert all(r["filled_quantity"] == 1 and r["status"] == "CANCELLED" for r in state(env)["reservations"])
    assert all(r.filled_quantity == 1 and r.status == "CANCELLED" for r in native(env).snapshot().reservations)


def test_authority_lagging_native_terminal_still_requests_exact_order_id(account_env):
    env = account_env
    assert run(env).submitted_orders == 2
    ledger = native(env)
    evidence = tuple(OrderEvidence("synthetic-terminal-" + r.reservation_id, r.reservation_id, ACCOUNT,
        env.now.isoformat(), r.broker_order_id, "CANCELLED", r.quantity, 0, 0, ()) for r in ledger.pending_reservations())
    env.now += pd.Timedelta(seconds=1)
    ledger.reconcile(PortfolioEvidence("lagging", ACCOUNT, env.now.isoformat(), env.held,
        {s: 100. for s in SYMBOLS}, {s: 15000. for s in SYMBOLS}, "lagging"), order_evidence=evidence)
    proxy = execution.evidence_ledger(env.root, ledger, env.config)
    assert {r.broker_order_id for r in proxy.pending_reservations()} == {"1001", "1002"}


def test_lost_account_plan_blocks_old_inventory_before_broker(account_env, monkeypatch):
    env = account_env
    assert run(env).submitted_orders == 2
    env.broker.calls.clear()
    monkeypatch.setattr(gameplan_execution, "account_execution_plan", lambda *a, **k: (_ for _ in ()).throw(ValueError("missing combined source")))
    result = run(env)
    assert result.status == "INDEPENDENT_TARGET_PLAN_UNAVAILABLE"
    assert env.broker.calls == []


def test_selected_source_fingerprint_and_fallback_are_bound(account_env):
    env = account_env
    assert runtime._loaded_gameplan_run(env.root, env.signals, (), execution_ready_plan=True) == env.plan.path
    wrong = {key: replace(s, source_fingerprint="another-run") for key, s in env.signals.items()}
    with pytest.raises(ValueError, match="differ from the selected"):
        runtime._loaded_gameplan_run(env.root, wrong, (), execution_ready_plan=True)
    with pytest.raises(ValueError, match="not the selected"):
        runtime._loaded_fallback_policy(env.root, env.root / "other", env.plan.report["action_date"])


def test_saved_account_fallback_binds_the_combined_receipt(account_env):
    from ml.artifacts import file_checksum
    from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
    env = account_env
    env.plan.report.update(action_date="2026-10-02", cross_horizon_fallback_policy=policy_for_action_date("2026-10-02"))
    policy, binding = runtime._loaded_fallback_policy(env.root, env.plan.path, "2026-10-02")
    assert policy == env.plan.report["cross_horizon_fallback_policy"]
    assert binding == {"source_run": "ml/account-gameplan-runs/combined-fixture",
                       "source_receipt_sha256": file_checksum(env.plan.path / "receipt.json")}


def test_changed_host_binding_blocks_even_cancellation_preparation(account_env):
    env = account_env
    assert run(env).submitted_orders == 2
    ledger = native(env)
    prior = ledger.snapshot()
    env.now += pd.Timedelta(hours=1)
    evidence = tuple(OrderEvidence("working-" + r.reservation_id, r.reservation_id, ACCOUNT,
        env.now.isoformat(), r.broker_order_id, "WORKING", r.quantity, 0, r.quantity, ()) for r in prior.reservations)
    ledger.reconcile(PortfolioEvidence("working", ACCOUNT, env.now.isoformat(), env.held,
        {s: 100. for s in SYMBOLS}, {s: 15000. for s in SYMBOLS}, "working"), order_evidence=evidence)
    original = env.config
    env.config = replace(original, machine_id="pc-new")
    env.broker.calls.clear()
    count, error = runtime._cancel_expired_entries(env.root, env.broker, ledger, ledger.snapshot(),
        SimpleNamespace(broker_identity_fingerprint="b"*64, observed_at=env.now.isoformat()), ACCOUNT,
        lambda: env.now, gameplan_entries=True, account_config=original)
    assert count == 0 and error
    assert env.broker.calls == [] and env.broker.cancellations == []


def test_external_pending_buy_is_account_wide_and_conservatively_valued():
    portfolio = SimpleNamespace(quotes={}, broker_working_orders=({"order_id": "1001", "symbol": "OTHER",
        "instruction": "BUY", "asset_type": "EQUITY", "status": "CURRENT", "remaining_quantity": 2,
        "filled_quantity": 1, "limit_price": 10.001, "reserved_cash": 20.002},))
    row, = execution._broker_pending(portfolio)
    assert row.symbol == "OTHER" and Decimal(row.exposure_reserved) == Decimal("20.02")

