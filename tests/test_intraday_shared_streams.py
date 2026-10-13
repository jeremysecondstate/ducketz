"""Shared native accounting keeps policy ownership exact across PCs, offline."""
from dataclasses import replace
from types import SimpleNamespace
import sqlite3

import pytest

from ml.account_gameplan.authority import AuthorityError
from ml.account_gameplan.execution import AccountExecutionContext
from ml.intraday.cancellation import CancellationService, gameplan_reservation
from ml.intraday.prediction import ContractError
from ml.intraday import runtime_compatibility as compatibility
from ml.stock_trader import independent_runtime
from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, LedgerError
from test_intraday_native_ledger import (
    ACCOUNT, NOW, TARGET, evidence, legacy_filled_lots, portfolio, reserve, retry,
)


def native(tmp_path):
    result = HorizonLedger(tmp_path / "native.sqlite3", ACCOUNT)
    assert result.reconcile(portfolio()).ready
    return result


def test_scout_attempts_keep_stream_and_disjoint_symbol_ownership(tmp_path):
    ledger = native(tmp_path)
    atlas = reserve(ledger, "atlas", symbol="AAPL")
    scout = reserve(ledger, "scout", symbol="AMZN", stream="scout-15m")
    assert ledger.reservation_stream(atlas.reservation_id) == "atlas-15m"
    assert ledger.reservation_stream(scout.reservation_id) == "scout-15m"
    assert ledger.allocation_stream(scout.allocation_id) == "scout-15m"
    assert ledger.intraday_attempts(symbol="AMZN", forecast_id=scout.forecast_id) == ()
    assert ledger.intraday_attempts(symbol="AMZN", forecast_id=scout.forecast_id,
                                   stream="scout-15m") == (scout,)
    assert not gameplan_reservation(ledger, scout)
    with pytest.raises(LedgerError, match="ALLOCATION_STREAM_MISMATCH"):
        reserve(ledger, "cross-owner", symbol="AAPL", stream="scout-15m")
    assert len(ledger.snapshot().reservations) == 2


@pytest.mark.parametrize("stream", ["gameplan", "unknown", "Scout", ""])
def test_unreviewed_stream_cannot_create_native_records(tmp_path, stream):
    ledger = native(tmp_path)
    with pytest.raises(LedgerError, match="UNREVIEWED_INTRADAY_STREAM"):
        reserve(ledger, stream=stream)
    assert ledger.snapshot().reservations == ()


def test_scout_retry_reuses_exact_proven_unsent_identity_without_cross_stream_fork(tmp_path):
    ledger = native(tmp_path)
    first = reserve(ledger, stream="scout-15m")
    ledger.mark_submission(first.reservation_id, status="REJECTED", observed_at=NOW,
                           evidence_id="proven-unsent")
    with pytest.raises(LedgerError, match="PREDECESSOR_SCOPE"):
        retry(ledger, first, "atlas-cannot-retry")
    second = retry(ledger, first, "scout-retry", stream="scout-15m", quantity=3)
    assert second.allocation_id == first.allocation_id
    assert ledger.intraday_attempts(symbol="AAPL", forecast_id=first.forecast_id,
                                   stream="scout-15m") == (ledger.lookup_reservation(first.reservation_id), second)
    ledger.mark_submission(second.reservation_id, status="UNKNOWN", observed_at=NOW,
                           evidence_id="ambiguous-send")
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    with pytest.raises(LedgerError, match="LATEST_PROVEN_UNSUBMITTED"):
        retry(reopened, second, "cannot-resend", stream="scout-15m")
    assert reopened.lookup_reservation(second.reservation_id).reserved_quantity == 3


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m", "future-explicit-stream"])
def test_gameplan_direct_mutation_guards_reject_every_explicit_stream(tmp_path, stream):
    ledger = native(tmp_path)
    order = reserve(ledger)
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE intraday_streams SET stream=?", (stream,))
    for operation in (ledger.reserve_cancellation, ledger.reserve_order_timeout):
        with pytest.raises(LedgerError, match="STREAM_MISMATCH"):
            operation(order.reservation_id, as_of=NOW, evidence_id="not-gameplan")
    with pytest.raises(LedgerError, match="TIMEOUT_RETRY_STREAM_MISMATCH"):
        ledger.bind_timeout_retry(order.reservation_id, order.reservation_id,
                                  requested_quantity=1, as_of=NOW, evidence_id="wrong-retry")
    assert ledger.lookup_reservation(order.reservation_id).cancel_requested_at is None


def test_scout_held_lots_never_receive_gameplan_scheduled_liquidation(tmp_path):
    ledger = native(tmp_path)
    order = reserve(ledger, stream="scout-15m", quantity=2)
    assert ledger.reconcile(portfolio("filled", "2026-10-12T17:15:05+00:00", held=22),
                            order_evidence=(evidence(order, "buy-filled", filled=2),)).ready
    assert ledger.reconcile(portfolio("later", TARGET, held=22)).ready
    assert ledger.due_exits(snapshot_id="later", as_of=TARGET) == ()
    with pytest.raises(LedgerError, match="NO_SCHEDULED_LIQUIDATION"):
        ledger.reserve_exit(allocation_id=order.allocation_id, quantity=1, limit_price=10,
                            snapshot_id="later", idempotency_key="wrong-exit", batch_id="wrong-exit", as_of=TARGET)


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_gameplan_never_abandons_another_stream_pre_send_record(tmp_path, stream):
    ledger = native(tmp_path)
    order = reserve(ledger, stream=stream)
    ledger.mark_submission(order.reservation_id, status="REJECTED", observed_at=NOW,
                           evidence_id="owner-stopped-before-send")
    record = dict(reservation_id="shared", status="RESERVED", broker_order_id=None,
                  request=dict(native_reservation_id=order.reservation_id, native_allocation_id=order.allocation_id),
                  filled_quantity=0, last_evidence_at=None)
    abandoned = []
    context = object.__new__(AccountExecutionContext)
    context.native_ledger = ledger
    context.lease = SimpleNamespace(token="test-lease")
    context.authority = SimpleNamespace(read_state=lambda: {"reservations": [record]},
                                       abandon_unsubmitted=lambda *args: abandoned.append(args))
    with pytest.raises(AuthorityError, match="FRESH_EXACT_AUTHORITY_ORDER_EVIDENCE_REQUIRED"):
        context._reconcile(())
    assert abandoned == []


def test_scout_cancellation_reuses_exact_multi_horizon_fill_and_terminal_recovery(tmp_path):
    ledger = HorizonLedger(tmp_path / "native.sqlite3", ACCOUNT)
    owners = legacy_filled_lots(ledger)
    order = reserve(ledger, "scout-sale", stream="scout-15m", side="SELL", quantity=12)
    broker_id = "broker-" + order.idempotency_key
    ledger.mark_submission(order.reservation_id, status="SUBMITTED", broker_order_id=broker_id,
                           observed_at="2026-10-12T17:15:04+00:00", evidence_id="accepted")
    clock = ["2026-10-12T17:15:06+00:00"]
    current = [evidence(order, "working", filled=0, status="WORKING", at="2026-10-12T17:15:05+00:00")]
    events, deletes = [], []
    broker = SimpleNamespace(stable_account_fingerprint=lambda: ACCOUNT,
                             prepare_order_submission=lambda: SimpleNamespace(identity_fingerprint=ACCOUNT))
    def cancel(identity, context, *, before_delete):
        before_delete()
        deletes.append(identity)
    broker.cancel_prepared_order = cancel
    def apply(observed):
        held = 12 - observed.cumulative_filled_quantity
        assert ledger.reconcile(portfolio("snapshot-" + observed.evidence_id, clock[0], held=held),
                                order_evidence=(observed,)).ready
    def adapter(stream):
        return CancellationService(broker=broker, ledger=ledger, stream=stream, account_fingerprint=ACCOUNT,
            before_cancel=lambda *_: None, apply_evidence=apply, record_event=lambda k,p: events.append((k,p)),
            clock=lambda: clock[0], observe=lambda _: current[0])
    with pytest.raises(ContractError, match="outside the selected"):
        adapter("atlas-15m").step(order.reservation_id, reason="new-forecast", evidence_id="wrong-owner")
    scout = adapter("scout-15m")
    first = scout.step(order.reservation_id, reason="new-forecast", evidence_id="scout-cancel")
    assert first.state == "pending" and deletes == [broker_id]
    assert ledger.lookup_reservation(order.reservation_id).reserved_quantity == 12
    assert all(payload["stream"] == "scout-15m" for _, payload in events)
    clock[0] = "2026-10-12T17:15:09+00:00"
    current[0] = evidence(order, "terminal", filled=5, status="CANCELLED", at="2026-10-12T17:15:08+00:00",
        fills=(FillEvidence("exact-fill-one", 3, 10, "2026-10-12T17:15:06+00:00"),
               FillEvidence("exact-fill-two", 2, 10, "2026-10-12T17:15:07+00:00")))
    # Reconstruct the policy service after interruption. The marker prevents a
    # second DELETE; exact cumulative terminal evidence releases only the rest.
    second = adapter("scout-15m").step(order.reservation_id, reason="new-forecast", evidence_id="resume")
    assert second.complete_terminal_evidence and deletes == [broker_id]
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert reopened.lookup_reservation(order.reservation_id).filled_quantity == 5
    assert sum(a.reserved_sell_shares for a in reopened.snapshot().allocations) == 0
    attribution = reopened.intraday_sell_attribution(order.reservation_id)
    assert sum(row["quantity"] for row in attribution["fills"]) == 5
    assert {row["allocation"] for row in attribution["slices"]} >= {owner.allocation_id for owner in owners}


@pytest.mark.parametrize("method", ["reserve_intraday", "_intraday_attempts", "reserve_cancellation",
                                     "reserve_order_timeout", "bind_timeout_retry", "reserve_exit"])
def test_loaded_proof_rejects_old_native_mutation_methods(monkeypatch, method):
    monkeypatch.setattr(HorizonLedger, method, lambda *_args, **_kwargs: None)
    proof = compatibility.emit_loaded_attestation(vars(independent_runtime), HorizonLedger,
        observed_at=NOW, session_managed=True, pid=44, process_created_at=1)
    assert proof["compatible"]
    from pathlib import Path
    assert proof["fingerprints"] != compatibility.expected_fingerprints(Path(__file__).resolve().parents[1])


def test_stream_evidence_projects_exact_fills_without_faking_portfolio_and_rejects_replay_changes(tmp_path):
    ledger = native(tmp_path)
    order = reserve(ledger, stream="scout-15m", quantity=2)
    observed = evidence(order, "observed", filled=1, status="PARTIAL")
    with pytest.raises(LedgerError, match="ORDER_EVIDENCE_STREAM_MISMATCH"):
        ledger.apply_stream_order_evidence(observed, stream="atlas-15m")
    assert ledger.lookup_reservation(order.reservation_id).filled_quantity == 0
    result = ledger.apply_stream_order_evidence(observed, stream="scout-15m")
    assert result.filled_quantity == 1 and result.reserved_quantity == 1
    assert ledger.apply_stream_order_evidence(observed, stream="scout-15m") == result
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    with pytest.raises(LedgerError, match="EVIDENCE_ID_REUSED"):
        ledger.apply_stream_order_evidence(replace(observed, cumulative_filled_quantity=2), stream="scout-15m")
    with pytest.raises(LedgerError, match="FILL_ID_REUSED"):
        ledger.apply_stream_order_evidence(replace(observed, evidence_id="changed-fill",
            observed_at="2026-10-12T17:15:05+00:00", fills=(replace(observed.fills[0], price=11),)), stream="scout-15m")
    assert ledger.lookup_reservation(order.reservation_id) == result
    assert ledger.reconcile(portfolio("fresh-account", "2026-10-12T17:15:06+00:00", held=21)).ready


def preclaim_record(order):
    return dict(reservation_id="shared-scout", status="RESERVED", broker_order_id=None,
        filled_quantity=0, last_evidence_at=None, request=dict(stream="scout-15m", participant_id="pc-new",
            account_fingerprint=ACCOUNT, remote_request_sha256="a"*64,
            native_reservation_id=order.reservation_id, native_allocation_id=order.allocation_id,
            symbol=order.symbol, side=order.side, quantity=order.quantity, forecast_id=order.forecast_id,
            horizon="15m", limit_price="10.00"))


def test_preclaim_filter_omits_only_canonical_unclaimed_scout_while_orphans_remain(tmp_path, monkeypatch):
    from ml.account_gameplan import execution
    ledger = native(tmp_path)
    scout = reserve(ledger, "scout-preclaim", stream="scout-15m")
    orphan = reserve(ledger, "orphan", symbol="AMZN", stream="scout-15m")
    record = preclaim_record(scout)
    claims, abandoned = [], []
    authority = SimpleNamespace(read_state=lambda: {"reservations": [record]},
        remote_claim=lambda *_args, **_kwargs: claims[0] if claims else None,
        abandon_unsubmitted=lambda *args: abandoned.append(args))
    config = SimpleNamespace(account_fingerprint=ACCOUNT, coordinator_id="pc-original", participants={})
    path = tmp_path/execution.AUTHORITY_PATH
    path.parent.mkdir(parents=True)
    path.touch()
    monkeypatch.setattr(execution, "AccountAuthority", lambda *_args: authority)
    assert execution.remote_preclaim_native(authority, record, scout, ledger=ledger)
    assert execution.evidence_ledger(tmp_path, ledger, config).pending_reservations() == (orphan,)
    context = object.__new__(AccountExecutionContext)
    context.native_ledger, context.authority = ledger, authority
    context.lease = SimpleNamespace(token="test-lease")
    context._reconcile(())
    assert abandoned == []
    claims.append({"claim_id": "possible-send"})
    assert not execution.remote_preclaim_native(authority, record, scout, ledger=ledger)
    assert {r.reservation_id for r in execution.evidence_ledger(tmp_path, ledger, config).pending_reservations()} == {
        scout.reservation_id, orphan.reservation_id}
    with pytest.raises(AuthorityError, match="FRESH_EXACT"):
        context._reconcile(())


@pytest.mark.parametrize("mismatch", ["request", "native-stream", "claimed", "native-unknown", "authority-submitting"])
def test_preclaim_filter_rejects_mismatch_and_any_possible_send(tmp_path, mismatch):
    from ml.account_gameplan.execution import remote_preclaim_native
    ledger = native(tmp_path)
    scout = reserve(ledger, stream="scout-15m")
    record = preclaim_record(scout)
    if mismatch == "request":
        record["request"]["quantity"] += 1
    elif mismatch == "native-stream":
        with sqlite3.connect(ledger.path) as db:
            db.execute("UPDATE intraday_streams SET stream='atlas-15m'")
    elif mismatch == "native-unknown":
        ledger.mark_submission(scout.reservation_id, status="UNKNOWN", observed_at=NOW, evidence_id="ambiguous")
        scout = ledger.lookup_reservation(scout.reservation_id)
    elif mismatch == "authority-submitting":
        record["status"] = "SUBMITTING"
    authority = SimpleNamespace(remote_claim=lambda *_a, **_k: {"claim_id": "existing"} if mismatch=="claimed" else None)
    assert not remote_preclaim_native(authority, record, scout, ledger=ledger)


def test_bound_atlas_executor_proof_matches_reviewed_source_without_constructing_runtime():
    from pathlib import Path
    from ml.intraday.account_execution import IntradayAccountExecutor
    executor = object.__new__(IntradayAccountExecutor)
    actual = compatibility.actual_intraday_executor_fingerprints(executor)
    expected = compatibility.expected_intraday_executor_fingerprints(Path(__file__).resolve().parents[1])
    assert actual == expected
    assert "ml/intraday/account_execution.py:IntradayAccountExecutor._account" in actual
    assert "ml/account_gameplan/authority.py:AccountAuthority._reconcile" in actual
    assert "ml/account_gameplan/authority.py:AccountAuthority._reserve_intraday" in actual


@pytest.mark.parametrize("method", ["_capture", "_repair_pre_send", "_submit", "recover"])
def test_bound_atlas_executor_proof_does_not_replace_old_loaded_method_with_fresh_module(method):
    from pathlib import Path
    from types import FunctionType
    from ml.intraday.account_execution import IntradayAccountExecutor
    original = getattr(IntradayAccountExecutor, method)
    module_code = compile("def old(self, *args, **kwargs): return None", "old-loaded-worker", "exec")
    old_code = next(value for value in module_code.co_consts if hasattr(value, "co_code"))
    old_function = FunctionType(old_code, original.__globals__)
    old_class = type("OldLoadedExecutor", (IntradayAccountExecutor,), {method: old_function})
    executor = object.__new__(old_class)
    expected = compatibility.expected_intraday_executor_fingerprints(Path(__file__).resolve().parents[1])
    assert compatibility.actual_intraday_executor_fingerprints(executor) != expected


def test_contextmanager_proof_fingerprints_retained_body_not_common_wrapper():
    from contextlib import contextmanager
    from pathlib import Path
    from types import FunctionType
    from ml.intraday.account_execution import IntradayAccountExecutor
    original = IntradayAccountExecutor._account.__wrapped__
    module_code = compile("def old(self): yield None", "old-loaded-account", "exec")
    old_code = next(value for value in module_code.co_consts if hasattr(value, "co_code"))
    old_function = FunctionType(old_code, original.__globals__)
    old_class = type("OldLoadedExecutor", (IntradayAccountExecutor,), {"_account": contextmanager(old_function)})
    executor = object.__new__(old_class)
    expected = compatibility.expected_intraday_executor_fingerprints(Path(__file__).resolve().parents[1])
    assert compatibility.actual_intraday_executor_fingerprints(executor) != expected


@pytest.mark.parametrize("method", ["_reserve_intraday", "_reconcile", "_remote_row", "_remote_claim",
                                     "claim_remote_send", "release_remote_unsubmitted"])
def test_gameplan_proof_requires_changed_authority_helper_bodies(monkeypatch, method):
    from pathlib import Path
    from ml.account_gameplan.authority import AccountAuthority
    monkeypatch.setattr(AccountAuthority, method, lambda *_args, **_kwargs: None)
    proof = compatibility.emit_loaded_attestation(vars(independent_runtime), HorizonLedger,
        observed_at=NOW, session_managed=True, pid=44, process_created_at=1)
    assert proof["compatible"]
    assert proof["fingerprints"] != compatibility.expected_fingerprints(Path(__file__).resolve().parents[1])


# Reuse the established synthetic observation adapters, not production brokers.
from test_intraday_account_execution import env


def reserve_scout_preclaim_in_executor(env, *, pair=True):
    from ml.account_gameplan.authority import IntradayReservationRequest
    forecast = env.forecast(symbol=env.config.participants["pc-new"][0])
    executor = env.executor
    with executor._account():
        executor._capture()
        order = executor.ledger.reserve_intraday(symbol=forecast["symbol"], forecast_id=forecast["forecast_id"],
            side="BUY", quantity=1, limit_price="50.00", snapshot_id=executor.snapshot_id,
            idempotency_key="scout-preclaim", batch_id="scout-preclaim", as_of=env.clock(),
            reference_at=forecast["reference_at"], target_close_at=forecast["target_close"],
            forecast_sha256=forecast["forecast_id"], forecast_snapshot_sha256=forecast["snapshot_hash"],
            local_symbols=env.config.participants["pc-new"], stream="scout-15m")
        if pair:
            shared = executor.authority.reserve_remote_intraday(executor.lease.token, IntradayReservationRequest(
                idempotency_key="shared-preclaim", snapshot_id=executor.snapshot_id, account_fingerprint=env.config.account_fingerprint,
                action_date=env.now.tz_convert("America/Los_Angeles").date().isoformat(), participant_id="pc-new",
                symbol=forecast["symbol"], forecast_id=forecast["forecast_id"], side="BUY", quantity=1,
                limit_price="50.00", exposure_price="50.00", native_reservation_id=order.reservation_id,
                native_allocation_id=order.allocation_id, forecast_sha256=forecast["forecast_id"],
                forecast_snapshot_sha256=forecast["snapshot_hash"], issued_at=forecast["issued_at"],
                reference_at=forecast["reference_at"], target_close_at=forecast["target_close"],
                target_version=forecast["target_version"], local_symbols=env.config.participants["pc-new"],
                stream="scout-15m"), request_sha256="f"*64)
        else:
            shared = None
    return order, shared


def test_atlas_fresh_capture_keeps_canonical_scout_preclaim_charged_without_broker_identity(env):
    order, shared = reserve_scout_preclaim_in_executor(env)
    with env.executor._account():
        assert env.executor._capture() == ()
        state = env.executor.authority.reservation(shared.reservation_id)
        assert state.status == "RESERVED" and state.broker_order_id is None
        capacity = env.executor.authority.available_intraday_capacity(env.executor.lease.token, env.executor.snapshot_id)
        assert float(capacity["cash_available"]) == 50
        assert env.executor.ledger.lookup_reservation(order.reservation_id).reserved_quantity == 1
        with sqlite3.connect(env.executor.ledger.path) as db:
            row = db.execute("SELECT ready,reasons FROM snapshots WHERE id=?", (env.executor.snapshot_id,)).fetchone()
            assert row[0] == 0 and order.reservation_id in row[1]
    assert env.broker.submissions == []


@pytest.mark.parametrize("condition", ["orphan", "unknown", "claimed", "inventory-discrepancy"])
def test_atlas_capture_does_not_exempt_orphan_uncertain_or_unrelated_native_failure(env, condition):
    import pandas as pd
    from ml.stock_trader.horizon_broker import OrderHistoryError
    order, shared = reserve_scout_preclaim_in_executor(env, pair=condition!="orphan")
    with env.executor._account():
        if condition == "unknown":
            env.executor.ledger.mark_submission(order.reservation_id, status="UNKNOWN", observed_at=env.clock(),
                                                evidence_id="send-outcome-uncertain")
        elif condition == "claimed":
            env.executor.authority.claim_remote_send(env.executor.lease.token, shared.reservation_id,
                participant_id="pc-new", request_sha256="f"*64, claim_id="claimed-before-loss",
                expires_at=(env.now+pd.Timedelta(seconds=10)).isoformat(), snapshot_id=env.executor.snapshot_id)
        elif condition == "inventory-discrepancy":
            with env.executor.ledger._transaction() as db:
                db.execute("INSERT INTO blocks VALUES (?,?)", (order.symbol,"UNEXPLAINED_MANUAL_OR_EXTERNAL_SHARE_REDUCTION"))
        expected = ContractError if condition=="inventory-discrepancy" else OrderHistoryError
        with pytest.raises(expected):
            env.executor._capture()
        assert env.executor.ledger.lookup_reservation(order.reservation_id).reserved_quantity == 1
    assert env.broker.submissions == []
