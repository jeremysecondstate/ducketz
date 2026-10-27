"""A new executor generation reuses real private journals with fake broker IO."""
from datetime import timedelta

import pytest

from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import ContractError
from ml.intraday.scout_journal import JournalError
from test_intraday_scout_local_execution import env, forecast, record, raw_order


def reopen(env, *, resume=True, session_id=None):
    old = env.executor
    old.journal.close()
    value = execution.ScoutLocalAccountExecutor(repository=env.root, datastore=env.root,
        profile=old.profile, store=env.store, broker=env.broker, local_symbols=old.symbols,
        session_started_at=old.session_started_at, session_id=session_id or old.session_id,
        clock=lambda: env.state.now, manual_authority=lambda: env.state.manual,
        stopped=lambda: env.state.stop, resume_session=resume)
    return value


@pytest.mark.parametrize("filled", [0, 1])
def test_rotation_keeps_working_order_reservation_partial_fill_and_original_forecast(env, filled):
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    env.state.now += timedelta(seconds=2)
    env.broker.orders = [raw_order(env, status="PARTIALLY_FILLED" if filled else "WORKING", filled=filled)]
    env.executor.recover()
    original = record(env)
    original_order = env.executor.journal.order_state(original["request"]["request_id"])
    journal_id, journal_path = env.executor.journal.journal_id, env.executor.journal.path
    original_forecast = env.store.forecast(env.forecast["cycle_id"], "DOCU")
    posts_before, deletes_before = len(env.broker.posts), len(env.broker.deletes)
    next_executor = reopen(env)
    next_executor.preflight()
    env.executor = next_executor
    assert env.executor.journal.journal_id == journal_id and env.executor.journal.path == journal_path
    assert record(env) == original
    assert env.executor.journal.order_state(original["request"]["request_id"]) == original_order
    assert original["broker_order_id"] == "123" and original["grant"]["reservation_id"]
    assert env.store.forecast(env.forecast["cycle_id"], "DOCU") == original_forecast
    env.executor.recover()
    result = env.executor.step(env.forecast)
    assert result["disposition"] == "recover_existing_intention"
    assert len(env.broker.posts) == posts_before and len(env.broker.deletes) == deletes_before
    assert env.executor.journal.order_state(original["request"]["request_id"])["evidence"]["cumulative_filled_quantity"] == filled


def test_rotation_retains_unknown_send_and_one_send_key_without_resubmission(env):
    env.broker.post_error = TimeoutError("fixture lost response")
    result = env.executor.step(env.forecast)
    assert result["submission_status"] == "UNKNOWN"
    before = record(env)
    assert before["state"] == "UNKNOWN"
    next_executor = reopen(env)
    next_executor.preflight()
    env.executor = next_executor
    assert record(env) == before
    env.broker.post_error = None
    env.executor.recover()
    env.executor.step(env.forecast)
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)["disposition"] == "submission_recovery_required"
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
    preserved = env.executor.journal.get(before["request"]["request_id"])
    assert preserved["state"] == "UNKNOWN" and preserved["send_token"] == before["send_token"]
    assert preserved["grant"]["reservation_id"] == before["grant"]["reservation_id"]


def test_rotation_reacquires_capacity_and_quotes_for_new_effect(env):
    env.cash = 0
    assert env.executor.step(env.forecast)["disposition"] == "no_purchasing_capacity"
    prior = env.captures
    next_executor = reopen(env)
    next_executor.preflight()
    env.executor = next_executor
    env.cash = 250
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    assert env.captures > prior and len(env.broker.posts) == 1


def test_rotation_never_replays_an_expired_forecast(env):
    next_executor = reopen(env)
    next_executor.preflight()
    env.executor = next_executor
    env.state.now += timedelta(hours=1)
    assert env.executor.step(env.forecast)["disposition"] == "fixed_target_already_elapsed"
    assert env.broker.posts == []


def test_successor_cannot_create_missing_journal_or_clear_a_stop(env):
    next_executor = reopen(env, session_id="missing-original-session")
    with pytest.raises(JournalError, match="Unknown original"):
        next_executor.preflight()
    assert "missing-original-session" not in next_executor.books.sessions()
    env.state.stop = True
    next_executor = reopen(env)
    with pytest.raises(ContractError, match="startup was stopped"):
        next_executor.preflight()
    assert env.broker.posts == []


def test_initial_activation_still_rejects_duplicate_logical_session(env):
    next_executor = reopen(env, resume=False)
    with pytest.raises(JournalError, match="already registered"):
        next_executor.preflight()
