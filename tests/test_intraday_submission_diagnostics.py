"""Exact final-gate clocks and timings from synthetic own submission failures."""
import json
import math
import sqlite3
import threading

import pandas as pd
import pytest

from test_intraday_account_execution import env, account, native, advance, persisted_failure
from ml.account_gameplan.authority import AccountAuthority, AuthorityError
from ml.intraday import account_execution as execution


@pytest.mark.parametrize("expiry", ["quote", "target"])
def test_final_gate_retains_exact_evaluated_clocks_without_sending(env, expiry):
    forecast = env.forecast()
    if expiry == "quote":
        env.quote_age = 59
        env.broker.before_post_hook = lambda: advance(env, 2)
    else:
        env.now = pd.Timestamp(forecast["target_close"]) - pd.Timedelta(seconds=1)
        env.broker.before_post_hook = lambda: setattr(env, "now", pd.Timestamp(forecast["target_close"]))
    result = env.executor.step(forecast)
    evidence = persisted_failure(env, forecast, result)
    gate = evidence["submission_final_gate"]
    quote = env.executor.portfolio.quotes[forecast["symbol"]]
    assert gate["clock_reached"] is True
    assert gate["checked_at"] == env.clock()
    assert gate["quote_observed_at"] == quote.freshness_observed_at
    assert gate["quote_age_seconds"] == (env.now - pd.Timestamp(quote.freshness_observed_at)).total_seconds()
    assert gate["maximum_quote_age_seconds"] == 60
    assert gate["target_close_at"] == forecast["target_close"]
    assert gate["seconds_to_target_close"] == (pd.Timestamp(forecast["target_close"]) - env.now).total_seconds()
    assert gate["snapshot_id"] == env.executor.snapshot_id
    assert gate["snapshot_observed_at"] == env.executor.portfolio.observed_at
    if expiry == "quote":
        assert gate["quote_age_seconds"] > 60
        assert evidence["submission_stage"] == "quote_freshness"
    else:
        assert gate["seconds_to_target_close"] == 0
        assert evidence["submission_stage"] == "forecast_eligibility"
    assert result["disposition"] == "not_submitted" and not env.broker.submissions
    assert all(row.submitted_at is None for row in native(env).snapshot().reservations)
    assert account(env).read_state()["reservations"][0]["status"] == "NOT_SUBMITTED"


def test_gate_context_uses_refreshed_execution_quote_not_frozen_sizing(env, monkeypatch):
    forecast = env.forecast()
    save = env.executor.intents.save_sizing
    sizing_quotes = []

    def slow_save(*args, **kwargs):
        sizing_quotes.append(env.executor.portfolio.quotes[forecast["symbol"]].freshness_observed_at)
        result = save(*args, **kwargs)
        # Model the existing same-fence worker during slow durable sizing.
        for _ in range(3):
            advance(env, 20)
            env.executor._lease_keeper.renew()
        advance(env, 1)
        env.quote_age = 59
        return result

    monkeypatch.setattr(env.executor.intents, "save_sizing", slow_save)
    env.broker.before_post_hook = lambda: advance(env, 2)
    result = env.executor.step(forecast)
    gate = result["evidence"]["submission_final_gate"]
    assert env.captures == 2
    assert gate["quote_observed_at"] != sizing_quotes[0]
    assert gate["quote_observed_at"] == env.executor.portfolio.quotes[forecast["symbol"]].freshness_observed_at
    assert gate["quote_age_seconds"] == 61
    assert result["desired_quantity"] == 4 and not env.broker.submissions


def test_slow_submission_phases_and_gate_clock_are_frozen_before_cleanup(env, monkeypatch):
    monkeypatch.setattr(execution.time, "monotonic", lambda: env.now.timestamp())
    forecast = env.forecast()
    env.quote_age = 59
    prepare = env.broker.prepare_order_submission
    reserve_native = execution.HorizonLedger.reserve_intraday
    reserve_account = AccountAuthority.reserve_intraday
    renew = AccountAuthority.renew_lease
    reservation = AccountAuthority.reservation
    verify = env.broker.verify_read_snapshot
    main_thread = threading.get_ident()
    in_final = [False]
    final_renewals = []
    failure_clock = []

    def delayed_prepare():
        result = prepare()
        advance(env, 3)
        return result

    def delayed_native(*args, **kwargs):
        result = reserve_native(*args, **kwargs)
        advance(env, 4)
        return result

    def delayed_account(*args, **kwargs):
        result = reserve_account(*args, **kwargs)
        advance(env, 5)
        return result

    def delayed_renew(*args, **kwargs):
        result = renew(*args, **kwargs)
        if in_final[0] and threading.get_ident() == main_thread:
            final_renewals.append(result)
            if len(final_renewals) == 2:
                advance(env, 7)
                failure_clock.append(env.clock())
        return result

    def delayed_lookup(*args, **kwargs):
        result = reservation(*args, **kwargs)
        advance(env, 20)
        return result

    def final_hook():
        in_final[0] = True
        def delayed_verify(identity):
            verify(identity)
            advance(env, 6)
        env.broker.verify_read_snapshot = delayed_verify

    monkeypatch.setattr(env.broker, "prepare_order_submission", delayed_prepare)
    monkeypatch.setattr(execution.HorizonLedger, "reserve_intraday", delayed_native)
    monkeypatch.setattr(AccountAuthority, "reserve_intraday", delayed_account)
    monkeypatch.setattr(AccountAuthority, "renew_lease", delayed_renew)
    monkeypatch.setattr(AccountAuthority, "reservation", delayed_lookup)
    env.broker.before_post_hook = final_hook
    result = env.executor.step(forecast)
    evidence = persisted_failure(env, forecast, result)
    timings = evidence["submission_timing"]
    phases = timings["phases"]
    for stage, seconds in (("prepare_order_submission", 3), ("reserve_native", 4),
                           ("reserve_account", 5), ("broker_snapshot_identity", 6),
                           ("current_manual_session_and_account", 7)):
        assert any(phase["stage"] == stage and phase["elapsed_seconds"] >= seconds for phase in phases)
    assert timings["elapsed_seconds"] == 25
    assert evidence["submission_final_gate"]["checked_at"] == failure_clock[0]
    assert pd.Timestamp(failure_clock[0]) < env.now
    assert evidence["submission_final_gate"]["quote_age_seconds"] == 84
    assert len(phases) <= 32 and not env.broker.submissions


@pytest.mark.parametrize("lease_expired", [False, True])
def test_identity_failure_retains_unreached_clock_and_original_no_send_cause(env, monkeypatch, lease_expired):
    monkeypatch.setattr(execution.time, "monotonic", lambda: env.now.timestamp())
    forecast = env.forecast()
    secret = "private-identity-fixture-token-and-account-url"
    failure = TimeoutError(secret)

    def final_hook():
        def fail(_identity):
            advance(env, 61 if lease_expired else 7)
            raise failure
        env.broker.verify_read_snapshot = fail

    env.broker.before_post_hook = final_hook
    result = env.executor.step(forecast)
    evidence = persisted_failure(env, forecast, result)
    gate = evidence["submission_final_gate"]
    assert gate["clock_reached"] is False
    assert gate["checked_at"] is gate["quote_age_seconds"] is gate["seconds_to_target_close"] is None
    assert gate["quote_observed_at"] and gate["snapshot_id"]
    assert evidence["submission_stage"] == "broker_snapshot_identity"
    assert evidence["submission_timing"]["stage"] == "broker_snapshot_identity"
    assert evidence["submission_timing"]["stage_elapsed_seconds"] == (61 if lease_expired else 7)
    assert evidence["root_cause"] == {"error_type": "TimeoutError"}
    assert secret not in json.dumps(evidence)
    if lease_expired:
        assert evidence["awaiting_fresh_authority_fence"] is True
        assert evidence["submission_receipt"]["outcome"] == "NOT_SUBMITTED"
        assert native(env).snapshot().reservations[0].status == "RESERVED"
    assert result["disposition"] == "not_submitted" and not env.broker.submissions


def test_cleanup_failure_does_not_replace_or_retime_original_failure(env, monkeypatch):
    monkeypatch.setattr(execution.time, "monotonic", lambda: env.now.timestamp())
    forecast = env.forecast()

    def build(*args, **kwargs):
        advance(env, 2)
        raise ValueError("private-payload-fixture")

    def abandon(*args, **kwargs):
        advance(env, 10)
        raise AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")

    monkeypatch.setattr(execution, "build_schwab_stock_order_payload", build)
    monkeypatch.setattr(AccountAuthority, "abandon_unsubmitted", abandon)
    result = env.executor.step(forecast)
    evidence = persisted_failure(env, forecast, result)
    assert evidence["root_cause"] == {"error_type": "ValueError"}
    assert evidence["submission_timing"]["stage"] == "build_order_payload"
    assert evidence["submission_timing"]["stage_elapsed_seconds"] == 2
    assert evidence["submission_timing"]["elapsed_seconds"] == 2
    assert evidence["submission_final_gate"]["clock_reached"] is False
    assert evidence["cleanup_failure"]["exception_chain"][0]["error_type"] == "AuthorityError"
    assert native(env).snapshot().reservations[0].status == "RESERVED"
    assert not env.broker.submissions


def test_prepare_failure_propagates_original_with_unreached_submission_context(env, monkeypatch):
    monkeypatch.setattr(execution.time, "monotonic", lambda: env.now.timestamp())
    forecast = env.forecast()
    failure = TimeoutError("private-prepare-fixture")

    def prepare():
        advance(env, 7)
        raise failure

    monkeypatch.setattr(env.broker, "prepare_order_submission", prepare)
    with pytest.raises(TimeoutError) as caught:
        env.executor.step(forecast)
    assert caught.value is failure
    evidence = execution.execution_failure_evidence(failure)
    assert evidence["submission_stage"] == "prepare_order_submission"
    assert evidence["submission_final_gate"]["clock_reached"] is False
    assert evidence["submission_timing"]["stage_elapsed_seconds"] == 7
    assert not native(env).snapshot().reservations and not env.broker.submissions
    assert "private-prepare" not in json.dumps(evidence)


@pytest.mark.parametrize("operation", ["reservation", "submission_receipt", "mark_submission", "lookup_reservation"])
def test_propagated_cleanup_failure_keeps_frozen_original_submission(env, monkeypatch, operation):
    forecast = env.forecast()
    env.quote_age = 59
    failing = [False]
    cleanup_error = RuntimeError("private-cleanup-read-or-write-fixture")
    owner = AccountAuthority if operation in {"reservation", "submission_receipt"} else execution.HorizonLedger
    original = getattr(owner, operation)

    def fail(*args, **kwargs):
        if failing[0]:
            raise cleanup_error
        return original(*args, **kwargs)

    def final_hook():
        advance(env, 2)
        failing[0] = True

    monkeypatch.setattr(owner, operation, fail)
    env.broker.before_post_hook = final_hook
    with pytest.raises(RuntimeError) as caught:
        env.executor.step(forecast)
    assert caught.value is cleanup_error
    evidence = execution.execution_failure_evidence(cleanup_error)
    assert evidence["exception_chain"][0]["error_type"] == "RuntimeError"
    origin = evidence["original_submission_failure"]
    assert origin["submission_stage"] == "quote_freshness"
    assert origin["root_cause"]["message"] == "Quote needs refresh before submission"
    assert origin["submission_final_gate"]["quote_age_seconds"] == 61
    assert origin["submission_final_gate"]["checked_at"] == env.clock()
    assert "private-cleanup" not in json.dumps(evidence)
    assert not env.broker.submissions


@pytest.mark.parametrize("failure_kind", ["unsent", "interrupted"])
def test_authority_exception_cleanup_does_not_inflate_original_stage(env, monkeypatch, failure_kind):
    monkeypatch.setattr(execution.time, "monotonic", lambda: env.now.timestamp())
    forecast = env.forecast()
    cleanup_pending = [False]
    cleanup_delays = []
    original_lease = AccountAuthority._lease
    original_receipt = AccountAuthority._record_submission_receipt
    expected_elapsed = 2 if failure_kind == "unsent" else 3

    def receipt(*args, **kwargs):
        if kwargs.get("outcome") == "NOT_SUBMITTED":
            advance(env, 10)
            cleanup_delays.append(True)
        return original_receipt(*args, **kwargs)

    def lease(*args, **kwargs):
        if cleanup_pending[0]:
            cleanup_pending[0] = False
            advance(env, 10)
            cleanup_delays.append(True)
        return original_lease(*args, **kwargs)

    monkeypatch.setattr(AccountAuthority, "_record_submission_receipt", receipt)
    monkeypatch.setattr(AccountAuthority, "_lease", lease)
    if failure_kind == "unsent":
        env.quote_age = 59
        env.broker.before_post_hook = lambda: advance(env, 2)
        result = env.executor.step(forecast)
        evidence = persisted_failure(env, forecast, result)
        assert not env.broker.submissions
    else:
        interruption = KeyboardInterrupt("private-delayed-authority-cleanup")

        def interrupted(payload, context, *, before_post):
            before_post()
            advance(env, 3)
            cleanup_pending[0] = True
            raise interruption

        monkeypatch.setattr(env.broker, "submit_prepared_order", interrupted)
        with pytest.raises(KeyboardInterrupt) as caught:
            env.executor.step(forecast)
        assert caught.value is interruption
        with sqlite3.connect(env.store.path) as db:
            raw, = db.execute("SELECT payload FROM events WHERE kind='submission_interrupted'").fetchone()
        evidence = json.loads(raw)["diagnostics"]
        assert native(env).snapshot().reservations[0].status == "RESERVED"
        assert account(env).read_state()["reservations"][0]["status"] == "UNKNOWN"
    assert cleanup_delays == [True]
    assert evidence["submission_timing"]["elapsed_seconds"] == expected_elapsed
    assert evidence["submission_timing"]["stage_elapsed_seconds"] == (0 if failure_kind == "unsent" else 3)
    gate = evidence["submission_final_gate"]
    assert (env.now - pd.Timestamp(gate["checked_at"])).total_seconds() == (10 if failure_kind == "unsent" else 13)


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_post_interruption_retains_exact_gate_and_timing_without_consuming_stop(env, monkeypatch, failure_type):
    monkeypatch.setattr(execution.time, "monotonic", lambda: env.now.timestamp())
    forecast = env.forecast()
    failure = failure_type("private-stop-fixture")
    posts = []
    checked_at = []

    def interrupted(payload, context, *, before_post):
        before_post()
        checked_at.append(env.clock())
        posts.append(payload)
        advance(env, 3)
        raise failure

    monkeypatch.setattr(env.broker, "submit_prepared_order", interrupted)
    with pytest.raises(failure_type) as caught:
        env.executor.step(forecast)
    assert caught.value is failure and len(posts) == 1
    with sqlite3.connect(env.store.path) as db:
        raw, = db.execute("SELECT payload FROM events WHERE kind='submission_interrupted'").fetchone()
    evidence = json.loads(raw)["diagnostics"]
    assert evidence["submission_stage"] == "broker_submission"
    assert evidence["submission_final_gate"]["clock_reached"] is True
    assert evidence["submission_final_gate"]["checked_at"] == checked_at[0]
    assert evidence["submission_timing"]["stage_elapsed_seconds"] == 3
    assert evidence["root_cause"]["error_type"] == failure_type.__name__
    assert "private-stop" not in raw
    assert account(env).read_state()["reservations"][0]["status"] == "UNKNOWN"
    assert native(env).snapshot().reservations[0].status == "RESERVED"


def test_diagnostic_clock_failure_cannot_replace_original_unsent_failure(env, monkeypatch):
    forecast = env.forecast()
    real_monotonic = execution.time.monotonic
    import sys

    def faulty_diagnostic_clock():
        if sys._getframe(1).f_code.co_name == "_now":
            raise ValueError("private-diagnostic-clock-fixture")
        return real_monotonic()

    monkeypatch.setattr(execution.time, "monotonic", faulty_diagnostic_clock)
    env.quote_age = 59
    env.broker.before_post_hook = lambda: advance(env, 2)
    result = env.executor.step(forecast)
    evidence = persisted_failure(env, forecast, result)
    assert evidence["root_cause"]["message"] == "Quote needs refresh before submission"
    assert evidence["submission_diagnostics_unavailable"] is True
    assert evidence["submission_final_gate"]["quote_age_seconds"] == 61
    assert "private-diagnostic" not in json.dumps(evidence, allow_nan=False)
    assert not env.broker.submissions


def test_submission_timing_is_bounded_finite_and_detached(monkeypatch):
    clock = [0.]
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock[0])
    trace = execution._SubmissionDiagnostics()
    for _ in range(50):
        clock[0] += 1
        trace.advance("broker_snapshot_identity")
    saved = trace.snapshot()
    assert len(saved["submission_timing"]["phases"]) == 32
    assert saved["submission_timing"]["elapsed_seconds"] == 50
    saved["submission_timing"]["phases"][-1]["elapsed_seconds"] = 999
    assert trace.snapshot()["submission_timing"]["phases"][-1]["elapsed_seconds"] == 1
    clock[0] = float("nan")
    trace.advance("broker_submission")
    clock[0] = float("inf")
    finite = trace.snapshot()
    assert finite["submission_diagnostics_unavailable"] is True
    assert finite["submission_timing"]["elapsed_seconds"] is None
    json.dumps(finite, allow_nan=False)
    assert all(item["elapsed_seconds"] is None or math.isfinite(item["elapsed_seconds"])
               for item in finite["submission_timing"]["phases"])
