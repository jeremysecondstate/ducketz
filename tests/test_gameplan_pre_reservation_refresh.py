"""Elapsed initial state recaptures; no reservation or possible send is replayed."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from test_account_gameplan_execution import account_env, run, native, state
from test_independent_stock_runtime import ACCOUNT, environment
from ml.account_gameplan import authority, execution
from ml.stock_trader import gameplan_execution, independent_runtime as runtime, independent_session as worker


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail("Gameplan refresh tests must never connect to a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def delay_initial_check(env, monkeypatch, *, seconds=65, after_delay=None):
    """Delay the first full source read after prepare, before any intention."""
    prepared, delayed = [False], [False]
    original_prepare = env.broker.prepare_order_submission
    def prepare():
        prepared[0] = True
        return original_prepare()
    monkeypatch.setattr(env.broker, "prepare_order_submission", prepare)
    def plan(root, **kwargs):
        if prepared[0] and not delayed[0]:
            delayed[0] = True
            env.now += pd.Timedelta(seconds=seconds)
            if after_delay:
                after_delay()
        return env.plan, env.config
    monkeypatch.setattr(gameplan_execution, "account_execution_plan", plan)


def assert_no_intention(env):
    assert env.broker.submissions == env.broker.cancellations == []
    assert native(env).snapshot().reservations == ()
    assert state(env)["reservations"] == []
    events = env.root / "ml/stock-trader-execution-events"
    assert not events.exists() or not list(events.iterdir())


def test_elapsed_own_fence_recaptures_and_submits_each_original_once(account_env, monkeypatch):
    env = account_env
    originals = {s.prediction_id: (s.calibrated_probability, s.target_window_start, s.target_window_end)
                 for s in env.signals.values()}
    delay_initial_check(env, monkeypatch)
    first = run(env)
    assert first.status == "BROKER_STATE_REFRESH_REQUIRED" and first.error is None
    assert not first.stopped_after_error and first.submitted_orders == 0
    assert_no_intention(env)
    frozen = (first.run_directory / "decisions.json").read_bytes()
    env.now += pd.Timedelta(milliseconds=1)
    second = run(env)
    assert second.status == "ORDERS_SUBMITTED", second.error
    assert second.submitted_orders == len(env.broker.submissions) == len(originals)
    reservations = native(env).snapshot().reservations
    assert len(reservations) == len({r.forecast_id for r in reservations}) == len(originals)
    assert {r.forecast_id for r in reservations} == set(originals)
    assert (first.run_directory / "decisions.json").read_bytes() == frozen
    for publication in (first, second):
        for decision in json.loads((publication.run_directory / "decisions.json").read_bytes())["decisions"]:
            p = decision["prediction"]
            assert (p["calibrated_probability"], p["target_window_start"], p["target_window_end"]) == originals[p["prediction_id"]]


def test_worker_keeps_initial_elapsed_fence_out_of_failure_counters(account_env, monkeypatch):
    env = account_env
    delay_initial_check(env, monkeypatch)
    monkeypatch.setattr(worker, "_has_inventory", lambda _: True)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda _: SimpleNamespace(active=env.active))
    results = []
    def runner(root, **kwargs):
        result = run(env)
        results.append(result)
        if len(results) == 2:
            env.active = False
        return result
    outcome = worker.run_independent_stock_session(env.root, execute=True, clock=lambda: env.now,
        sleep=lambda seconds: setattr(env, "now", env.now + pd.Timedelta(seconds=seconds)),
        runner=runner, reporter=lambda _: None, sizing_policy=worker.GAMEPLAN_SIZING_POLICY)
    assert len(results) == env.captures == 2
    assert [r.status for r in results] == ["BROKER_STATE_REFRESH_REQUIRED", "ORDERS_SUBMITTED"]
    saved = json.loads((env.root / "state/independent-stock-trader/session-status.json").read_bytes())
    assert saved["failed_cycles"] == saved["consecutive_failures"] == 0
    assert outcome["orders_submitted"] == len(env.broker.submissions) == 2


@pytest.mark.parametrize("evidence", ["portfolio", "quote"])
def test_state_aging_during_pre_intent_validation_requests_complete_recapture(account_env, monkeypatch, evidence):
    env = account_env
    if evidence == "quote":
        env.portfolio_transform = lambda p: replace(p, quotes={s: replace(q,
            observed_at=(env.now - pd.Timedelta(seconds=59)).isoformat()) for s, q in p.quotes.items()})
    original = runtime._assert_execution_deployment
    delayed = [False]
    def validate(*args, **kwargs):
        original(*args, **kwargs)
        if "prepare" in env.broker.calls and not delayed[0]:
            delayed[0] = True
            env.now += pd.Timedelta(seconds=65 if evidence == "portfolio" else 2)
    monkeypatch.setattr(runtime, "_assert_execution_deployment", validate)
    first = run(env)
    assert first.status == "BROKER_STATE_REFRESH_REQUIRED" and first.error is None
    assert_no_intention(env)


@pytest.mark.parametrize("boundary", ["manual_stop", "expired_target"])
def test_elapsed_fence_does_not_hide_manual_stop_or_expired_target(account_env, monkeypatch, boundary):
    env = account_env
    if boundary == "expired_target":
        env.signals = {key: replace(s, actionable_until=(env.now + pd.Timedelta(seconds=30)).isoformat())
                       for key, s in env.signals.items()}
    delay_initial_check(env, monkeypatch,
        after_delay=(lambda: setattr(env, "active", False)) if boundary == "manual_stop" else None)
    first = run(env)
    assert first.status == "SUBMISSION_STOPPED_SAFETY_CHECK", first.error
    assert first.error and ("OPERATOR_INTENT" in first.error if boundary == "manual_stop" else "PREDICTION_TARGET_SAFETY_BOUNDARY_REACHED" in first.error)
    assert_no_intention(env)
    env.now += pd.Timedelta(milliseconds=1)
    assert run(env).submitted_orders == 0
    assert env.broker.submissions == []
    if boundary == "manual_stop":
        assert not env.active


@pytest.mark.parametrize("change", ["takeover", "renewed_expiry", "source", "config", "clock_rollback"])
def test_elapsed_state_never_hides_fencing_or_changed_binding(account_env, monkeypatch, change):
    env = account_env
    original_config = env.config
    def damage():
        if change == "source":
            env.plan = SimpleNamespace(**{**vars(env.plan), "manifest_sha256": "8" * 64})
        elif change == "config":
            env.config = replace(env.config, account_fingerprint="9" * 64, fingerprint="7" * 64)
        elif change == "clock_rollback":
            env.now -= pd.Timedelta(seconds=66)
        elif change == "takeover":
            other = authority.AccountAuthority(env.root / execution.AUTHORITY_PATH, execution._binding(env.config),
                clock=lambda: env.now.to_pydatetime())
            other.acquire_lease(coordinator_id=env.config.machine_id, holder_id="different-owned-cycle")
    if change == "renewed_expiry":
        original = authority.AccountAuthority.renew_lease
        calls = [0]
        def renew(self, token, **kwargs):
            calls[0] += 1
            if calls[0] == 3:
                # Same token, but an expiry unknown to this context is not its exact lease.
                with self._transaction() as db:
                    db.execute("UPDATE lease SET expires=? WHERE singleton=1",
                               ((env.now - pd.Timedelta(seconds=1)).isoformat(),))
            return original(self, token, **kwargs)
        monkeypatch.setattr(authority.AccountAuthority, "renew_lease", renew)
    delay_initial_check(env, monkeypatch, after_delay=damage)
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK", result.error
    assert result.error and result.submitted_orders == 0
    env.config = original_config  # Inspect the unchanged fake database under its original binding.
    assert_no_intention(env)


@pytest.mark.parametrize("field,damage", [(field, damage) for field in ("portfolio", "quote")
    for damage in ("missing", "future", "invalid", "malformed")])
def test_expired_lease_does_not_normalize_invalid_initial_evidence(account_env, monkeypatch, field, damage):
    env = account_env
    from ml.stock_trader import gameplan_direction_engine
    build = gameplan_direction_engine.build_gameplan_direction_trade_decisions
    def damaged(*args, **kwargs):
        decisions = build(*args, **kwargs)
        stamp = (None if damage == "missing" else (env.now + pd.Timedelta(seconds=1)).isoformat()
                 if damage == "future" else "invalid")
        changed = []
        for decision in decisions:
            if not decision.quantity:
                changed.append(decision)
                continue
            values = dict(getattr(decision, field))
            if damage == "malformed":
                if field == "quote":
                    values["unexpected_field"] = True
                else:
                    values.pop("observed_at")
            else:
                values["observed_at"] = stamp
            changed.append(replace(decision, **{field: values}))
        return tuple(changed)
    monkeypatch.setattr(gameplan_direction_engine, "build_gameplan_direction_trade_decisions", damaged)
    delay_initial_check(env, monkeypatch)
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK", result.error
    assert "CURRENT_FENCED_LEASE_REQUIRED" in result.error
    assert_no_intention(env)


def test_expiry_after_reservation_retains_original_no_send_reconciliation(account_env):
    env = account_env
    env.broker.before_post_hook = lambda: setattr(env, "now", env.now + pd.Timedelta(seconds=65))
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_AFTER_ERROR", result.error
    assert "uncertain" in result.error
    assert env.broker.submissions == []
    assert [r.status for r in native(env).snapshot().reservations] == ["UNKNOWN"]
    assert [r["status"] for r in state(env)["reservations"]] == ["SUBMITTING"]


def test_expired_error_does_not_expose_the_private_token(account_env):
    env = account_env
    own = authority.AccountAuthority(env.root / execution.AUTHORITY_PATH, execution._binding(env.config),
        clock=lambda: env.now.to_pydatetime())
    lease = own.acquire_lease(coordinator_id=env.config.machine_id, holder_id="own-cycle")
    env.now += pd.Timedelta(seconds=61)
    with pytest.raises(authority.AuthorityError) as failure:
        own.renew_lease(lease.token)
    assert type(failure.value) is authority.AuthorityError
    assert authority.matches_current_lease_expiry(failure.value, lease)
    assert str(failure.value) == "CURRENT_FENCED_LEASE_REQUIRED"
    assert lease.token not in str(failure.value)
    assert lease.token not in repr(failure.value)
    assert lease.token not in repr(failure.value.current_lease_expiry)


@pytest.mark.parametrize("damage", ["token", "epoch", "holder_id", "expires_at", "wrong_marker", "wrong_exception", "wrong_message", "missing_marker"])
def test_expiry_proof_requires_the_exact_lease_and_typed_marker(account_env, damage):
    env = account_env
    own = authority.AccountAuthority(env.root / execution.AUTHORITY_PATH, execution._binding(env.config),
        clock=lambda: env.now.to_pydatetime())
    lease = own.acquire_lease(coordinator_id=env.config.machine_id, holder_id="own-cycle")
    env.now += pd.Timedelta(seconds=61)
    with pytest.raises(authority.AuthorityError) as raised:
        own.renew_lease(lease.token)
    error = raised.value
    if damage in {"token", "epoch", "holder_id", "expires_at"}:
        value = {"token": "different-token", "epoch": lease.epoch + 1, "holder_id": "different-holder",
                 "expires_at": (pd.Timestamp(lease.expires_at) + pd.Timedelta(seconds=1)).isoformat()}[damage]
        lease = replace(lease, **{damage: value})
    elif damage == "wrong_marker":
        error.current_lease_expiry = SimpleNamespace(lease=lease)
    elif damage == "wrong_exception":
        error = RuntimeError("CURRENT_FENCED_LEASE_REQUIRED")
        error.current_lease_expiry = raised.value.current_lease_expiry
    elif damage == "wrong_message":
        error.args = ("AUTHORITY_CLOCK_MOVED_BACKWARDS",)
    else:
        del error.current_lease_expiry
    assert not authority.matches_current_lease_expiry(error, lease)
