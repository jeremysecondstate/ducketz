"""Temporary real account/native ledgers with fake broker observations only."""
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from test_independent_stock_runtime import Broker, ACCOUNT, BROKER_ID, SYMBOLS
from test_intraday_starter_sizing import fixture as sizing_fixture
from ml.account_gameplan.authority import AccountAuthority, AuthorityBinding
from ml.intraday.local_execution import LocalExecutionBinding
from ml.intraday import account_execution as execution
from ml.intraday import cancellation
from ml.intraday.prediction import ContractError, digest
from ml.intraday.store import ShadowStore
from ml.stock_trader.contracts import PortfolioState, QuoteState
from ml.stock_trader.horizon_broker import OrderHistoryError
from ml.stock_trader.horizon_ledger import HorizonLedger, OrderEvidence, FillEvidence


@pytest.fixture
def env(tmp_path, monkeypatch):
    original, _, _, _ = sizing_fixture()
    state = SimpleNamespace(root=tmp_path, now=pd.Timestamp(original["issued_at"]), active=True,
        equity=15_000., cash=100., held=dict.fromkeys(SYMBOLS, 0.), broker=Broker(), orders={},
        captures=0, capture_fail=None, quote_age=0.)
    state.config = LocalExecutionBinding("Atlas", "pc-original", tuple(SYMBOLS[:4]), ACCOUNT, "e" * 64)
    state.store = ShadowStore(tmp_path / "intraday.sqlite3")
    state.session_started_at = state.now - pd.Timedelta(seconds=30)
    state.clock = lambda: state.now.isoformat()
    monkeypatch.setattr(execution, "load_local_binding", lambda _: state.config)

    def observe(broker, ledger, **kwargs):
        state.now += pd.Timedelta(milliseconds=1)
        rows = []
        for reservation in ledger.pending_reservations():
            if not reservation.broker_order_id:
                raise OrderHistoryError("UNRESOLVED_SUBMISSION_WITHOUT_EXACT_BROKER_ORDER_ID")
            row = state.orders[reservation.broker_order_id]
            filled = row.get("filled", 0)
            fills = (() if not filled else (FillEvidence("fill-" + reservation.broker_order_id, filled,
                reservation.limit_price, row["fill_at"]),))
            remaining = 0 if row["status"] in {"FILLED", "CANCELLED", "REJECTED"} else reservation.quantity - filled
            rows.append(OrderEvidence(digest([reservation.reservation_id, row, state.now.isoformat()]),
                reservation.reservation_id, ACCOUNT, state.now.isoformat(), reservation.broker_order_id,
                row["status"], reservation.quantity, filled, remaining, fills,
                "CANCELED" if row["status"] == "CANCELLED" else row["status"]))
        return tuple(rows)
    monkeypatch.setattr(execution, "capture_order_evidence", observe)
    monkeypatch.setattr(cancellation, "capture_order_evidence", observe)

    def capture(broker, **kwargs):
        if state.capture_fail:
            raise state.capture_fail
        state.captures += 1
        state.now += pd.Timedelta(milliseconds=1)
        working = []
        pending_buys, pending_sells = {}, {}
        for order_id, row in state.orders.items():
            if row["status"] not in {"WORKING", "PARTIAL", "SUBMITTED"}:
                continue
            left = row["quantity"] - row.get("filled", 0)
            amount = str(Decimal(row["price"]) * left if row["side"] == "BUY" else Decimal(0))
            working.append(dict(status="CURRENT", asset_type="EQUITY", instruction=row["side"],
                order_id=order_id, symbol=row["symbol"], remaining_quantity=left,
                filled_quantity=row.get("filled", 0), limit_price=row["price"], reserved_cash=amount))
            pending = pending_buys if row["side"] == "BUY" else pending_sells
            pending[row["symbol"]] = pending.get(row["symbol"], 0) + left
        observed = state.now.isoformat()
        quote_time = (state.now - pd.Timedelta(seconds=state.quote_age)).isoformat()
        return PortfolioState(observed, state.equity, state.cash, sum(state.held.values()) * 50, 0., dict(state.held),
            {s: state.held[s] * 50 for s in SYMBOLS}, pending_buys, pending_sells, len(working),
            {s: QuoteState(s, 50., 50., 50., 50., 1000., quote_time) for s in SYMBOLS},
            digest([observed, state.cash, state.held, working]), BROKER_ID, tuple(working))
    monkeypatch.setattr(execution, "capture_portfolio_state", capture)

    submit = state.broker.submit_prepared_order
    def fake_post(payload, context, *, before_post):
        location = submit(payload, context, before_post=before_post)
        broker_id = str(location).rsplit("/", 1)[-1]
        leg = payload["orderLegCollection"][0]
        state.orders[broker_id] = {"symbol": leg["instrument"]["symbol"], "side": leg["instruction"],
            "quantity": leg["quantity"], "price": payload["price"], "status": "WORKING"}
        return location
    state.broker.submit_prepared_order = fake_post
    state.executor = execution.IntradayAccountExecutor(datastore=tmp_path, store=state.store,
        broker=state.broker, local_symbols=state.config.participants["pc-original"],
        session_started_at=state.session_started_at, clock=state.clock,
        manual_authority=lambda: state.active)

    def forecast(*, offset=0, probability=.72, mode="live", symbol="AAPL"):
        raw, snapshot, _, _ = sizing_fixture(symbol=symbol, probability=probability)
        delta = pd.Timedelta(minutes=offset)
        for candle in snapshot["candles"]:
            for name in ("opened_at", "available_at"):
                candle[name] = (pd.Timestamp(candle[name]) + delta).isoformat()
        snapshot["cutoff"] = (pd.Timestamp(snapshot["cutoff"]) + delta).isoformat()
        snapshot["inference"]["reference_at"] = (pd.Timestamp(snapshot["inference"]["reference_at"]) + delta).isoformat()
        for name in ("reference_at", "data_through", "target_open", "target_close", "issued_at", "training_cutoff"):
            raw[name] = (pd.Timestamp(raw[name]) + delta).isoformat()
        cycle_id = digest([symbol, raw["reference_at"], probability, mode])
        state.store.begin_cycle(cycle_id, {"pc": state.config.actor, "symbols": [symbol], "reference_at": raw["reference_at"],
            "mode": mode, "configuration_hash": "fixture-only", "started_at": raw["issued_at"]})
        frozen_hash, _ = state.store.freeze_inputs(cycle_id, symbol, snapshot)
        raw.update(pc=state.config.actor, cycle_id=cycle_id, mode=mode,
            availability_basis="assumed_historical_close" if mode == "historical_replay" else "observed",
            information_available_at=snapshot["candles"][-1]["available_at"], snapshot_hash=frozen_hash,
            configured_count=10, successful_count=10, ensemble_status="full",
            models=[{"name": "fixture-model-" + str(i), "status": "success", "probability_not_down": probability,
                     "version": "fixture-only-no-training"} for i in range(10)])
        return state.store.save_forecast(raw)
    state.forecast = forecast
    return state


def native(env):
    return HorizonLedger(env.root / execution.NATIVE_PATH, ACCOUNT)


def account(env):
    return AccountAuthority(env.root / execution.AUTHORITY_PATH,
                            AuthorityBinding(ACCOUNT, env.config.coordinator_id, env.config.participants))


def advance(env, seconds=1):
    env.now += pd.Timedelta(seconds=seconds)


def persisted_failure(env, forecast, result):
    failures = [row for row in env.executor.intents.history(forecast["forecast_id"])
                if row["disposition"] == "not_submitted"]
    assert failures[-1]["evidence"] == result["evidence"]
    return failures[-1]["evidence"]


def test_four_desired_at_fifty_cash_hundred_submits_two_and_keeps_forecast(env):
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted"
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 2
    assert len(env.broker.submissions) == 1
    assert env.broker.submissions[0]["orderLegCollection"][0]["quantity"] == 2
    assert env.broker.submissions[0]["price"] == "50.00"
    assert env.store.forecasts()[0] == forecast
    history = env.executor.intents.history(forecast["forecast_id"])
    assert any(row["kind"] == "capacity" and row["desired_quantity"] == 4 and row["adjusted_quantity"] == 2 for row in history)
    assert result["filled_quantity"] == 0 and result["realized_pnl"] is None
    with account(env)._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0  # No Gameplan publication prerequisite.


def test_same_forecast_status_recovery_never_repeats_submission(env):
    forecast = env.forecast()
    env.executor.step(forecast)
    identity = native(env).snapshot().reservations[0].reservation_id
    advance(env)
    result = env.executor.step(forecast)
    assert result["disposition"] == "recover_existing_intention"
    assert len(env.broker.submissions) == 1
    assert native(env).snapshot().reservations[0].reservation_id == identity
    assert account(env).read_state()["reservations"][0]["broker_order_id"] == "1001"


def test_zero_cash_logs_positive_desired_zero_adjusted_without_post(env):
    env.cash = 0
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 0
    assert env.broker.submissions == [] and native(env).snapshot().reservations == ()
    assert result["submitted_quantity"] == result["filled_quantity"] == 0 and result["realized_pnl"] is None


def test_capacity_recovery_keeps_original_desired_size_when_equity_changes(env):
    env.cash = 0
    forecast = env.forecast()
    result = env.executor.step(forecast)
    original = env.executor.intents.get(forecast["forecast_id"])
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 0
    env.cash, env.equity = 200., 30_000.
    advance(env)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted"
    assert result["desired_quantity"] == result["adjusted_quantity"] == 4
    assert env.executor.intents.get(forecast["forecast_id"]) == original
    assert len(env.broker.submissions) == 1


def test_no_shares_preserves_sell_intention_and_forecast(env):
    forecast = env.forecast(probability=.2)
    result = env.executor.step(forecast)
    assert result["side"] == "SELL" and result["desired_quantity"] > 0 and result["adjusted_quantity"] == 0
    assert env.broker.submissions == []
    assert env.store.forecasts()[0] == forecast


def test_positive_sell_clips_only_to_actual_eligible_shares_even_with_no_cash(env):
    env.cash, env.equity, env.held["AAPL"] = 0., 100_000., 1.
    forecast = env.forecast(probability=.2)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted" and result["side"] == "SELL"
    assert result["desired_quantity"] > 1 and result["adjusted_quantity"] == 1
    assert len(env.broker.submissions) == 1
    assert env.broker.submissions[0]["orderLegCollection"][0]["instruction"] == "SELL"
    reservation = native(env).snapshot().reservations[0]
    assert reservation.side == "SELL" and reservation.quantity == 1
    assert account(env).read_state()["reservations"][0]["request"]["side"] == "SELL"
    assert result["filled_quantity"] == 0 and result["realized_pnl"] is None


def test_unknown_post_retains_both_commitments_and_never_guesses_or_reposts(env):
    env.broker.unknown_submission = True
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["disposition"] == "submission_recovery_required"
    assert result["submission_status"] == "UNKNOWN" and result["submitted_quantity"] is None
    assert [r.status for r in native(env).snapshot().reservations] == ["UNKNOWN"]
    assert [r["status"] for r in account(env).read_state()["reservations"]] == ["UNKNOWN"]
    advance(env)
    with pytest.raises(OrderHistoryError, match="WITHOUT_EXACT"):
        env.executor.step(forecast)
    assert len(env.broker.submissions) == 1 and env.broker.cancellations == []


def test_context_is_prepared_and_account_identity_is_checked_before_post(env):
    env.broker.prepared_identity = "d" * 64
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["disposition"] == "not_submitted" and env.broker.submissions == []
    assert [r.status for r in native(env).snapshot().reservations] == ["REJECTED"]
    assert [r["status"] for r in account(env).read_state()["reservations"]] == ["NOT_SUBMITTED"]
    assert "prepare" in env.broker.calls and "verify_snapshot" in env.broker.calls


def test_strict_cutoff_is_rechecked_in_final_post_gate(env):
    forecast = env.forecast(offset=345)  # Issue 16:45 Pacific; no exact-cutoff POST.
    env.now = pd.Timestamp(forecast["issued_at"])
    result = env.executor.step(forecast)
    assert result["disposition"] == "submission_cutoff" and env.broker.submissions == []


def test_cutoff_crossed_between_preparation_and_post_does_not_submit(env):
    fresh = env.forecast(offset=330)
    env.now = pd.Timestamp(fresh["issued_at"]).normalize() + pd.Timedelta(hours=23, minutes=44, seconds=59, milliseconds=500)
    env.broker.before_post_hook = lambda: setattr(env, "now", env.now.normalize() + pd.Timedelta(hours=23, minutes=45))
    result = env.executor.step(fresh)
    assert env.broker.submissions == [] and result["disposition"] == "not_submitted"


def test_manual_authority_revoked_in_final_post_gate_releases_only_unsent(env):
    forecast = env.forecast()
    env.broker.before_post_hook = lambda: setattr(env, "active", False)
    result = env.executor.step(forecast)
    assert result["disposition"] == "not_submitted" and env.broker.submissions == []
    assert [r.status for r in native(env).snapshot().reservations] == ["REJECTED"]
    assert [r["status"] for r in account(env).read_state()["reservations"]] == ["NOT_SUBMITTED"]
    assert env.store.forecasts()[0] == forecast
    evidence = persisted_failure(env, forecast, result)
    assert evidence["submission_stage"] == "current_manual_session_and_account"
    assert evidence["root_cause"] == {"error_type": "ContractError", "message": "Manual 15m session is no longer active"}
    assert not env.active


def test_pre_manual_forecast_is_recorded_without_account_read_or_post(env):
    forecast = env.forecast()
    env.executor.session_started_at = env.now + pd.Timedelta(seconds=1)
    result = env.executor.step(forecast)
    assert result["disposition"] == "forecast_precedes_manual_start"
    assert env.captures == 0 and env.broker.calls == []
    assert env.store.forecasts()[0] == forecast


@pytest.mark.parametrize("mode", ["shadow", "historical_replay"])
def test_old_shadow_or_replay_cannot_cancel_a_live_intention(env, mode):
    live = env.forecast()
    env.executor.step(live)
    unrelated = env.forecast(offset=15, mode=mode)
    env.now = pd.Timestamp(unrelated["issued_at"])
    env.executor.step(unrelated)
    assert env.broker.cancellations == [] and len(env.broker.submissions) == 1


def test_older_late_live_forecast_cannot_cancel_newer_live_intention(env):
    newer = env.forecast(offset=15)
    env.now = pd.Timestamp(newer["issued_at"])
    env.executor.step(newer)
    older = env.forecast()
    env.executor.step(older)
    assert env.broker.cancellations == [] and len(env.broker.submissions) == 1


def test_older_late_live_forecast_cannot_trade_after_newer_zero_intention(env):
    env.equity = 100
    newer = env.forecast(offset=15)
    env.now = pd.Timestamp(newer["issued_at"])
    zero = env.executor.step(newer)
    assert zero["desired_quantity"] == zero["adjusted_quantity"] == 0
    assert native(env).snapshot().reservations == ()
    env.equity = 15_000.
    older = env.forecast()
    result = env.executor.step(older)
    assert result["disposition"] == "newer_or_same_reference_intention_already_processed"
    assert env.broker.cancellations == [] and env.broker.submissions == []


def test_new_zero_intention_cancels_only_unfilled_prior_and_preserves_partial_fill(env):
    forecast = env.forecast()
    env.executor.step(forecast)
    original_id = native(env).snapshot().reservations[0].reservation_id
    env.orders["1001"].update(status="PARTIAL", filled=1, fill_at=env.clock())
    env.held["AAPL"] = 1
    env.cash = 50
    env.equity = 100
    new = env.forecast(offset=15)
    env.now = pd.Timestamp(new["issued_at"])
    result = env.executor.step(new)
    assert result["disposition"] == "previous_intention_reconciliation"
    assert env.executor.intents.get(new["forecast_id"])["desired_quantity"] == 0
    assert env.broker.cancellations == ["1001"]
    assert native(env).lookup_reservation(original_id).status == "PARTIAL"  # DELETE ack is not terminal.
    advance(env)
    assert env.executor.step(new)["disposition"] == "previous_intention_reconciliation"
    assert env.broker.cancellations == ["1001"]  # Await exact status after the durable acknowledgment.
    env.orders["1001"]["status"] = "CANCELLED"
    advance(env)
    result = env.executor.step(new)
    assert result["desired_quantity"] == result["adjusted_quantity"] == 0
    assert len(env.broker.submissions) == 1
    old = native(env).lookup_reservation(original_id)
    assert old.status == "CANCELLED" and old.filled_quantity == 1
    assert native(env).snapshot().allocations[0].filled_shares == 1
    assert account(env).read_state()["reservations"][0]["filled_quantity"] == 1


def test_gameplan_order_files_are_not_read_or_changed_by_intraday(env):
    foreign = env.root / "state/independent-stock-trader/holdings.sqlite3"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"unreadable other trader database")
    env.orders["9001"] = {"symbol":"AAPL", "side":"BUY", "quantity":1, "price":"50", "status":"WORKING"}
    env.cash = 50
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 1
    env.equity = 100
    new = env.forecast(offset=15)
    env.now = pd.Timestamp(new["issued_at"])
    env.executor.step(new)
    assert "9001" not in env.broker.cancellations
    assert foreign.read_bytes() == b"unreadable other trader database"
    assert all(r.broker_order_id != "9001" for r in native(env).snapshot().reservations)


def test_accepted_authority_id_survives_crash_before_native_acceptance(env, monkeypatch):
    forecast = env.forecast()
    original = HorizonLedger.mark_submission
    def crash(self, reservation_id, **kwargs):
        if kwargs["status"] == "SUBMITTED":
            raise SystemExit("synthetic crash after authority acceptance")
        return original(self, reservation_id, **kwargs)
    monkeypatch.setattr(HorizonLedger, "mark_submission", crash)
    with pytest.raises(SystemExit, match="authority acceptance"):
        env.executor.step(forecast)
    assert native(env).snapshot().reservations[0].broker_order_id is None
    assert account(env).read_state()["reservations"][0]["broker_order_id"] == "1001"
    monkeypatch.setattr(HorizonLedger, "mark_submission", original)
    advance(env)
    result = env.executor.step(forecast)
    assert result["disposition"] == "recover_existing_intention"
    assert native(env).snapshot().reservations[0].broker_order_id == "1001"
    assert len(env.broker.submissions) == 1


def test_temporary_portfolio_failure_preserves_forecast_without_fake_intention(env):
    forecast = env.forecast()
    env.capture_fail = TimeoutError("synthetic read timeout")
    with pytest.raises(TimeoutError):
        env.executor.step(forecast)
    assert env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.broker.submissions == [] and env.store.forecasts()[0] == forecast
    env.capture_fail = None
    advance(env)
    assert env.executor.step(forecast)["disposition"] == "submitted"


def test_newer_published_reference_during_account_capture_prevents_old_post(env, monkeypatch):
    old = env.forecast()
    env.now += pd.Timedelta(minutes=15)
    original = execution.capture_portfolio_state
    published = []
    def capture(*args, **kwargs):
        if not published:
            new = env.forecast(offset=15)
            published.append(new)
        return original(*args, **kwargs)
    monkeypatch.setattr(execution,"capture_portfolio_state",capture)
    assert env.executor.step(old)["disposition"] == "newer_or_same_reference_intention_already_processed"
    assert not env.broker.submissions and not env.broker.cancellations
    assert env.executor.step(published[0])["disposition"] == "submitted"


def test_newer_reference_published_at_final_gate_prevents_old_post(env):
    old = env.forecast()
    env.now += pd.Timedelta(minutes=15)
    def publish():
        env.forecast(offset=15)
    env.broker.before_post_hook = publish
    result = env.executor.step(old)
    assert result["disposition"] == "not_submitted"
    assert not env.broker.submissions
    assert account(env).read_state()["reservations"][0]["status"] == "NOT_SUBMITTED"


def test_proven_unsent_retry_refreshes_capacity_preserves_desired_and_all_attempts(env):
    forecast = env.forecast()
    original_identity=env.broker.verify_read_snapshot
    # A transient final identity read fails before the final send gate, with an
    # explicit no-POST proof. It must never become a filled/repeated purchase.
    def fail_final():
        env.broker.verify_read_snapshot=lambda identity: (_ for _ in ()).throw(TimeoutError("pre-send read"))
    env.broker.before_post_hook=fail_final
    first=env.executor.step(forecast)
    assert first["disposition"] == "not_submitted" and first["evidence"]["proven_unsent_retry_eligible"]
    assert not env.broker.submissions
    evidence = persisted_failure(env, forecast, first)
    assert evidence["submission_stage"] == "broker_snapshot_identity"
    assert [item["error_type"] for item in evidence["exception_chain"]] == ["SubmissionNotStarted", "TimeoutError"]
    assert evidence["root_cause"] == {"error_type": "TimeoutError"}
    assert not evidence["exception_chain_truncated"]
    env.broker.verify_read_snapshot=original_identity
    env.broker.before_post_hook=None
    env.cash=50
    env.equity=1000
    advance(env,4)
    second=env.executor.step(forecast)
    assert second["disposition"] == "submitted"
    assert second["desired_quantity"] == 4 and second["adjusted_quantity"] == 1
    attempts=native(env).intraday_attempts(symbol="AAPL",forecast_id=forecast["forecast_id"])
    assert len(attempts)==2 and attempts[0].status=="REJECTED" and attempts[1].broker_order_id=="1001"
    advance(env)
    env.executor.step(forecast)
    assert len(env.broker.submissions)==1


def test_native_only_unsent_admission_retries_without_fake_authority_parent(env,monkeypatch):
    forecast=env.forecast()
    original=AccountAuthority.reserve_intraday
    def fail(*args,**kwargs):
        raise TimeoutError("pre-admission fixture")
    monkeypatch.setattr(AccountAuthority,"reserve_intraday",fail)
    assert env.executor.step(forecast)["disposition"] == "not_submitted"
    assert account(env).read_state()["reservations"] == []
    monkeypatch.setattr(AccountAuthority,"reserve_intraday",original)
    advance(env,4)
    assert env.executor.step(forecast)["disposition"] == "submitted"
    assert len(env.broker.submissions)==1


def test_unavailable_new_sizing_still_retires_previous_unfilled_intention(env,monkeypatch):
    from ml.intraday.technical_sizing import SizingResult
    from ml.intraday.prediction import encoded
    old=env.forecast()
    assert env.executor.step(old)["disposition"]=="submitted"
    new=env.forecast(offset=15,probability=.25)
    env.now=pd.Timestamp(new["issued_at"])
    def unavailable(forecast,**kwargs):
        value=dict(forecast_id=forecast["forecast_id"],symbol="AAPL",side="SELL",status="unavailable",
            desired_quantity=None,calculated_at=env.clock(),reason="undefined_indicator_fixture")
        return SizingResult(digest(value),encoded(value))
    monkeypatch.setattr(execution,"size_forecast",unavailable)
    assert env.executor.step(new)["disposition"]=="previous_intention_reconciliation"
    assert env.broker.cancellations==["1001"]
    env.orders["1001"]["status"]="CANCELLED"
    advance(env)
    assert env.executor.step(new)["kind"]=="sizing_unavailable"
    assert len(env.broker.submissions)==1


def test_exact_accepted_response_after_lease_expiry_recovers_without_another_post(env):
    forecast=env.forecast()
    submit=env.broker.submit_prepared_order
    def delayed_receipt(payload,context,*,before_post):
        location=submit(payload,context,before_post=before_post)
        advance(env,61)
        return location
    env.broker.submit_prepared_order=delayed_receipt
    first=env.executor.step(forecast)
    assert first["disposition"]=="submission_recovery_required"
    assert len(env.broker.submissions)==1
    native_id=native(env).snapshot().reservations[0].reservation_id
    authority=account(env)
    authority_id=authority.read_state()["reservations"][0]["reservation_id"]
    assert authority.submission_receipt(authority_id)["broker_order_id"]=="1001"
    advance(env)
    second=env.executor.step(forecast)
    assert second["disposition"]=="recover_existing_intention"
    assert len(env.broker.submissions)==1
    repaired=native(env).lookup_reservation(native_id)
    assert repaired.broker_order_id=="1001" and repaired.status=="WORKING"
    assert account(env).reservation(authority_id).broker_order_id=="1001"


def test_audited_no_post_after_lease_expiry_retries_under_fresh_fence(env):
    forecast=env.forecast()
    verify=env.broker.verify_read_snapshot
    def expire_before_gate():
        def slow_identity(_identity):
            advance(env,61)
            raise TimeoutError("pre-POST identity read fixture")
        env.broker.verify_read_snapshot=slow_identity
    env.broker.before_post_hook=expire_before_gate
    first=env.executor.step(forecast)
    assert first["disposition"]=="not_submitted"
    assert first["evidence"]["proven_unsent_retry_eligible"]
    assert first["evidence"]["awaiting_fresh_authority_fence"]
    assert not env.broker.submissions
    evidence = persisted_failure(env, forecast, first)
    assert evidence["submission_stage"] == "broker_snapshot_identity"
    assert [item["error_type"] for item in evidence["exception_chain"]] == ["AuthorityError", "SubmissionNotStarted", "TimeoutError"]
    assert evidence["root_cause"] == {"error_type": "TimeoutError"}
    prior=native(env).snapshot().reservations[0]
    assert prior.status=="RESERVED" and prior.submitted_at is None
    env.broker.verify_read_snapshot=verify
    env.broker.before_post_hook=None
    env.cash=50
    advance(env)
    second=env.executor.step(forecast)
    assert second["disposition"]=="submitted" and second["desired_quantity"]==4
    assert second["adjusted_quantity"]==1 and len(env.broker.submissions)==1
    attempts=native(env).intraday_attempts(symbol="AAPL",forecast_id=forecast["forecast_id"])
    assert len(attempts)==2 and attempts[0].status=="REJECTED" and attempts[0].submitted_at is None
    assert attempts[1].broker_order_id=="1001"


@pytest.mark.parametrize("expiry,stage,message", [
    ("quote", "quote_freshness", "Quote needs refresh before submission"),
    ("target", "forecast_eligibility", "Forecast eligibility changed before submission"),
])
def test_prepost_expiry_cause_persists_without_submission(env, expiry, stage, message):
    forecast = env.forecast()
    if expiry == "quote":
        env.quote_age = 59
        env.broker.before_post_hook = lambda: advance(env, 2)
    else:
        env.now = pd.Timestamp(forecast["target_close"]) - pd.Timedelta(seconds=1)
        env.broker.before_post_hook = lambda: setattr(env, "now", pd.Timestamp(forecast["target_close"]))
    result = env.executor.step(forecast)
    assert result["disposition"] == "not_submitted" and not env.broker.submissions
    evidence = persisted_failure(env, forecast, result)
    assert evidence["submission_stage"] == stage
    assert evidence["root_cause"] == {"error_type": "ContractError", "message": message}
    assert all(row.submitted_at is None for row in native(env).snapshot().reservations)


def test_prepost_provider_diagnostics_do_not_persist_secrets(env):
    import json
    forecast = env.forecast()
    secret = "private-fixture-bearer-and-account-url"
    failure = TimeoutError(secret)
    failure.stock_trader_operation = "account_identity"
    failure.response = SimpleNamespace(status_code=503, headers={"Authorization": secret}, text=secret)
    def fail_final():
        env.broker.verify_read_snapshot = lambda _: (_ for _ in ()).throw(failure)
    env.broker.before_post_hook = fail_final
    result = env.executor.step(forecast)
    evidence = persisted_failure(env, forecast, result)
    assert evidence["root_cause"] == {"error_type": "TimeoutError", "operation": "account_identity", "http_status": 503}
    assert secret not in json.dumps(env.executor.intents.history(forecast["forecast_id"]))
    assert not env.broker.submissions


def test_submission_diagnostics_bound_cycles_and_reject_unreviewed_local_messages():
    import json
    secret = "private-fixture-local-message"
    error = execution.ContractError(secret)
    error.stock_trader_operation = secret
    error.__cause__ = error
    result = execution._submission_failure_evidence(error, "fixture")
    assert len(result["exception_chain"]) == 1 and result["exception_chain_truncated"]
    assert secret not in json.dumps(result)
    error.stock_trader_operation = {"untrusted": secret}
    assert secret not in json.dumps(execution._submission_failure_evidence(error, "fixture"))
    error = TimeoutError(secret)
    for _ in range(12):
        outer = execution.SubmissionNotStarted(secret)
        outer.__cause__ = error
        error = outer
    result = execution._submission_failure_evidence(error, "fixture")
    assert len(result["exception_chain"]) == 8 and result["exception_chain_truncated"]
    assert secret not in json.dumps(result)


def test_old_native_filled_shared_submitted_recovers_whole_second_fill_without_reposting(env, monkeypatch):
    from dataclasses import replace
    import json
    from ml.stock_trader.horizon_broker import normalize_order_evidence
    from ml.stock_trader.horizon_ledger import PortfolioEvidence
    from intraday_order_fixtures import precision_order

    forecast = env.forecast()
    assert env.executor.step(forecast)["disposition"] == "submitted"
    ledger = native(env)
    reservation, = ledger.snapshot().reservations
    shared = account(env)
    with shared._transaction() as db:
        created = db.execute("SELECT created FROM reservations").fetchone()[0]
    assert pd.Timestamp(created).microsecond > 0  # Reproduce genuine subsecond local creation.
    wire_time = pd.Timestamp(created).floor("s").strftime("%Y-%m-%dT%H:%M:%S+0000")
    raw = precision_order(reservation, wire_time)
    advance(env, 2)
    old = replace(normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT,
                  observed_at=env.clock()), fill_time_provenance=None)
    env.orders[reservation.broker_order_id].update(status="FILLED", filled=reservation.quantity, fill_at=wire_time)
    env.held[reservation.symbol] = reservation.quantity
    env.cash = 0.
    assert ledger.reconcile(PortfolioEvidence("native-before-shared", ACCOUNT, env.clock(), env.held,
        dict.fromkeys(SYMBOLS, 50.), dict.fromkeys(SYMBOLS, 15_000.), "fixture-native-before-shared"),
        order_evidence=(old,)).ready
    assert ledger.lookup_reservation(reservation.reservation_id).status == "FILLED"
    assert shared.read_state()["reservations"][0]["status"] == "SUBMITTED"
    with ledger._transaction() as db:
        old_fill_row = tuple(db.execute("SELECT id,reservation,quantity,price,executed_at FROM fills").fetchone())
        old_evidence_bytes = db.execute("SELECT payload FROM evidence WHERE id=?", (old.evidence_id,)).fetchone()[0]
    assert "fill_time_provenance" not in json.loads(old_evidence_bytes)
    requested_ids = []

    def fresh_capture(broker, proxy, **kwargs):
        pending = proxy.pending_reservations()
        requested_ids.extend(item.broker_order_id for item in pending)
        env.now += pd.Timedelta(milliseconds=1)
        return tuple(normalize_order_evidence(raw, item, account_fingerprint=ACCOUNT,
                     observed_at=env.clock()) for item in pending)

    monkeypatch.setattr(execution, "capture_order_evidence", fresh_capture)
    advance(env)
    result = env.executor.step(forecast)
    assert result["disposition"] == "recover_existing_intention"
    assert requested_ids == [reservation.broker_order_id]
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
    assert shared.read_state()["reservations"][0]["status"] == "FILLED"
    assert shared.read_state()["reservations"][0]["filled_quantity"] == reservation.quantity
    with ledger._transaction() as db:
        assert tuple(db.execute("SELECT id,reservation,quantity,price,executed_at FROM fills").fetchone()) == old_fill_row
        assert db.execute("SELECT payload FROM evidence WHERE id=?", (old.evidence_id,)).fetchone()[0] == old_evidence_bytes
    with shared._transaction() as db:
        fill = json.loads(db.execute("SELECT payload FROM fills").fetchone()[0])
        recorded = json.loads(db.execute("SELECT payload FROM order_evidence ORDER BY rowid DESC").fetchone()[0])
    assert fill == vars(old.fills[0])
    assert recorded["fill_time_provenance"]["fills"][fill["fill_id"]]["raw_timestamp"] == wire_time
    advance(env)
    env.executor.step(forecast)
    assert len(env.broker.submissions) == 1  # A completed purchase is never restored/repeated.


def test_scout_uses_own_stream_and_independent_local_authority(env):
    env.config = LocalExecutionBinding("Scout", "pc-new", env.config.symbols, ACCOUNT, "f"*64)
    env.executor = execution.IntradayAccountExecutor(datastore=env.root,store=env.store,broker=env.broker,
        local_symbols=env.config.symbols,session_started_at=env.session_started_at,clock=env.clock,
        manual_authority=lambda:env.active,actor="Scout")
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted" and len(env.broker.submissions)==1
    order = native(env).snapshot().reservations[0]
    assert native(env).reservation_stream(order.reservation_id)=="scout-15m"
    assert account(env).read_state()["reservations"][0]["request"]["stream"]=="scout-15m"
    advance(env)
    assert env.executor.step(forecast)["disposition"]=="recover_existing_intention"
    assert len(env.broker.submissions)==1
    env.equity=100
    newer=env.forecast(offset=15)
    env.now=pd.Timestamp(newer["issued_at"])
    result=env.executor.step(newer)
    assert result["disposition"]=="previous_intention_reconciliation"
    assert order.broker_order_id in env.broker.cancellations


def test_external_account_reduction_does_not_create_cross_trader_reconciliation(env):
    env.held["AAPL"] = 8
    with env.executor._account():
        env.executor._capture()
    advance(env)
    env.held["AAPL"] = 2
    forecast = env.forecast(probability=.28)
    result = env.executor.step(forecast)
    assert result["disposition"]=="submitted" and 0 < result["adjusted_quantity"] <= 2


def test_unrelated_forecast_history_cannot_expire_submission_quote(env, monkeypatch):
    for offset in range(-150, 0, 15):
        env.forecast(symbol="AMZN", offset=offset)
    forecast = env.forecast()
    env.quote_age = 35
    validate = ShadowStore._forecast_value
    unrelated_reads = []

    def timed_validation(row):
        value = validate(row)
        if value["symbol"] != forecast["symbol"]:
            unrelated_reads.append(value["forecast_id"])
            advance(env, 1)
        return value

    monkeypatch.setattr(ShadowStore, "_forecast_value", staticmethod(timed_validation))
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted", result.get("evidence")
    assert not unrelated_reads
    assert len(env.broker.submissions) == 1
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 2
    advance(env)
    assert env.executor.step(forecast)["disposition"] == "recover_existing_intention"
    assert len(env.broker.submissions) == 1
