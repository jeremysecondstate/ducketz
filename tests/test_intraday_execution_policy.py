"""Offline decision plans do not invoke providers, brokers or order submission."""
import ast
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pytest

from ml.intraday.book import Capacity, Fill, OfflineBook, OrderEvidence, Request
from ml.intraday.execution_policy import plan_new_forecast, submission_gate
from ml.intraday.prediction import ContractError, encoded

OBSERVED = "2026-10-12T17:00:00+00:00"
NOW = "2026-10-12T16:44:59-07:00"
SESSION = date(2026, 10, 12)


def ledger(tmp_path):
    result = OfflineBook(tmp_path/"offline-intraday.sqlite3", account_id="fixture-account",
                         atlas_symbols=("AAPL", "IONQ"), gameplan_symbols=("AAPL", "IONQ", "ABCL"))
    result.capacity(Capacity("fixture-capacity", OBSERVED, "10000", "10000",
                            {"AAPL": "10000", "IONQ": "10000", "ABCL": "10000"}))
    return result


def intent(request_id="new", forecast_id="forecast-new", **kwargs):
    return Request(request_id, forecast_id, "AAPL", "BUY", 4, "100", "atlas-15m", **kwargs)


def submit_fixture(book, request=None, *, status="WORKING", fill_quantity=0):
    request = request or intent("old", "forecast-old")
    book.reserve(request, admission_id="fixture-admission-"+request.request_id)
    book.begin_submit(request.request_id)
    fills = (Fill("fill-"+request.request_id, fill_quantity, "100", OBSERVED),) if fill_quantity else ()
    evidence = OrderEvidence("status-"+request.request_id, request.request_id, "broker-"+request.request_id,
                             OBSERVED, status, request.desired, fills)
    book.apply(evidence)
    return request


def plan(book, *, new=None, as_of=NOW, eligible_session=SESSION):
    return plan_new_forecast(book.snapshot(), intent=new or intent(), as_of=as_of, eligible_session=eligible_session)


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-12T16:44:59.999999-07:00", "before_submission_cutoff"),
    ("2026-10-12T16:45:00-07:00", "submission_cutoff"),
    ("2026-10-12T16:45:00.000001-07:00", "submission_cutoff"),
    ("2026-10-12T23:45:00Z", "submission_cutoff"),
    ("2026-10-12T23:44:59Z", "before_submission_cutoff"),
    ("2026-10-13T01:00:00Z", "submission_cutoff"), # Still October 12 in Pacific.
    ("2026-10-13T07:00:00Z", "wrong_session_date"),
])
def test_strict_pacific_cutoff_and_local_eligible_date(timestamp, expected):
    result = submission_gate(as_of=timestamp, eligible_session=SESSION)
    assert result.state == expected and result.cutoff_at == "2026-10-12T23:45:00+00:00"


@pytest.mark.parametrize("day,cutoff", [
    (date(2026, 3, 6), "2026-03-07T00:45:00+00:00"),
    (date(2026, 3, 9), "2026-03-09T23:45:00+00:00"),
    (date(2026, 10, 30), "2026-10-30T23:45:00+00:00"),
    (date(2026, 11, 2), "2026-11-03T00:45:00+00:00"),
])
def test_cutoff_uses_pacific_dst(day, cutoff):
    result = submission_gate(as_of=cutoff, eligible_session=day)
    assert result.state == "submission_cutoff" and result.cutoff_at == cutoff


def test_cutoff_is_not_market_calendar_or_opening_permission():
    result = submission_gate(as_of="2026-10-11T00:01:00-07:00", eligible_session=date(2026, 10, 11))
    assert result.state == "before_submission_cutoff" # Caller must separately establish an eligible session.
    assert "market" in result.reason
    with pytest.raises(ContractError, match="Timezone-aware"):
        submission_gate(as_of="2026-10-12T16:00:00", eligible_session=SESSION)
    with pytest.raises(ContractError, match="eligible session date"):
        submission_gate(as_of=NOW, eligible_session="2026-10-12")


def test_no_prior_order_ready_only_for_other_checks(tmp_path):
    result = plan(ledger(tmp_path))
    assert result.state == "ready_for_other_checks" and result.prior_orders == ()
    assert not hasattr(result, "quantity") and not hasattr(result, "order_payload")
    assert "shared capacity" in result.reason


def test_working_cancellation_acknowledgment_does_not_release_commitment(tmp_path):
    book = ledger(tmp_path)
    submit_fixture(book)
    result = plan(book)
    assert result.state == "reconcile_required"
    assert result.prior_orders[0].action == "retrieve_status_then_cancel_unfilled"
    assert result.prior_orders[0].unfilled_commitment_retained
    book.cancel_requested("old")
    pending = plan(book)
    assert pending.state == "reconcile_required"
    assert pending.prior_orders[0].action == "reconcile_status_and_cancellation"
    # Complete broker status still says working after a cancel acknowledgment.
    book.apply(OrderEvidence("still-working", "old", "broker-old", OBSERVED, "WORKING", 4, ()))
    assert plan(book).state == "reconcile_required"


def test_intervening_partial_fill_then_confirmed_cancellation_preserves_fills(tmp_path):
    book = ledger(tmp_path)
    submit_fixture(book)
    book.cancel_requested("old")
    fill = Fill("intervening-fill", 2, "100", OBSERVED)
    book.apply(OrderEvidence("partial-during-cancel", "old", "broker-old", OBSERVED, "PARTIAL", 4, (fill,)))
    partial = plan(book)
    assert partial.state == "reconcile_required" and partial.prior_orders[0].preserved_fills == (fill,)
    book.cancel_requested("old")
    book.apply(OrderEvidence("confirmed-cancel", "old", "broker-old", OBSERVED, "CANCELLED", 4, (fill,)))
    snapshot = encoded(book.snapshot())
    independent_new = replace(intent(), desired=9, side="SELL", limit="99")
    ready = plan(book, new=independent_new)
    assert ready.state == "ready_for_other_checks"
    assert ready.prior_orders[0].preserved_fills == (fill,) and not ready.prior_orders[0].unfilled_commitment_retained
    assert independent_new.desired == 9 # No old remainder or subtraction dictates the new forecast quantity.
    assert encoded(book.snapshot()) == snapshot
    assert book.snapshot()["allocations"]["purchase:old"]["held"] == 2
    # Cancelling the unfilled portion does not release the two filled BUY costs
    # before a coherent balance observation accounts for those executions.
    cash_probe = replace(intent("cash-probe", "forecast-cash-probe"), desired=100)
    assert book.reserve(cash_probe, admission_id="offline-cash-accounting-check")["permitted"] == 98


@pytest.mark.parametrize("status", ["UNKNOWN", "SUBMITTING", "CANCEL_PENDING", "RESERVED"])
def test_unresolved_prior_order_survives_restart_snapshot_and_cutoff(tmp_path, status):
    book = ledger(tmp_path)
    old = intent("old", "forecast-old")
    book.reserve(old, admission_id="fixture")
    if status != "RESERVED":
        book.begin_submit("old")
    if status == "UNKNOWN":
        book.unknown("old")
    if status == "CANCEL_PENDING":
        book.apply(OrderEvidence("working", "old", "broker-old", OBSERVED, "WORKING", 4, ()))
        book.cancel_requested("old")
    restarted = OfflineBook(book.path, account_id="fixture-account", atlas_symbols=("AAPL", "IONQ"),
                            gameplan_symbols=("AAPL", "IONQ", "ABCL"))
    result = plan(restarted, as_of="2026-10-12T16:50:00-07:00")
    assert result.state == "reconcile_required"
    assert result.submission_time.state == "submission_cutoff"
    assert result.prior_orders[0].unfilled_commitment_retained
    assert restarted.order("old")["status"] == status
    assert result.prior_orders[0].action == ("reconcile_unsent_reservation" if status == "RESERVED" else "reconcile_status_and_cancellation")


@pytest.mark.parametrize("status,filled", [("FILLED", 4), ("CANCELLED", 0), ("REJECTED", 0), ("NOT_ACCEPTED", 0)])
def test_resolved_terminal_order_never_replenishes_or_blocks_new_intention(tmp_path, status, filled):
    book = ledger(tmp_path)
    submit_fixture(book, status=status, fill_quantity=filled)
    result = plan(book)
    assert result.state == "ready_for_other_checks"
    assert result.prior_orders[0].action == "preserve_terminal_evidence"
    assert len(result.prior_orders[0].preserved_fills) == (1 if filled else 0)
    assert plan(book, as_of="2026-10-12T16:45:00-07:00").state == "submission_cutoff"
    assert plan(book, eligible_session=date(2026, 10, 13)).state == "wrong_session_date"


def test_unrecognized_status_fails_closed_and_retains_commitment(tmp_path):
    book = ledger(tmp_path)
    book.reserve(intent("old", "forecast-old"), admission_id="fixture")
    snapshot = book.snapshot()
    snapshot["orders"]["old"]["status"] = "NEW_UNRECOGNIZED_STATUS"
    original = deepcopy(snapshot)
    result = plan_new_forecast(snapshot, intent=intent(), as_of=NOW, eligible_session=SESSION)
    assert result.state == "invalid_order_state" and result.prior_orders[0].unfilled_commitment_retained
    existing = plan_new_forecast(snapshot, intent=intent("old", "forecast-old"), as_of=NOW, eligible_session=SESSION)
    assert existing.state == "invalid_order_state" and existing.prior_orders[0].unfilled_commitment_retained
    assert snapshot == original


def test_gameplan_and_other_symbol_intentions_are_separate(tmp_path):
    book = ledger(tmp_path)
    book.reserve(replace(intent("gameplan", "forecast-gameplan"), stream="gameplan", horizon="1h"), admission_id="fixture-gameplan")
    book.begin_submit("gameplan")
    book.unknown("gameplan")
    book.reserve(replace(intent("other", "forecast-other"), symbol="IONQ"), admission_id="fixture-other")
    book.begin_submit("other")
    book.unknown("other")
    result = plan(book)
    assert result.state == "ready_for_other_checks" and result.prior_orders == ()
    assert book.order("gameplan")["status"] == book.order("other")["status"] == "UNKNOWN"


def test_identity_recovery_prevents_duplicates_and_changed_intention(tmp_path):
    book = ledger(tmp_path)
    existing = intent()
    book.reserve(existing, admission_id="fixture")
    result = plan(book, new=existing)
    assert result.state == "existing_intention" and result.prior_orders[0].action == "recover_existing_intention"
    with pytest.raises(ContractError, match="changed intention"):
        plan(book, new=replace(existing, desired=5))
    with pytest.raises(ContractError, match="already has a persistent intention"):
        plan(book, new=replace(existing, request_id="duplicate"))
    with pytest.raises(ContractError, match="same-forecast remainder"):
        plan(book, new=replace(existing, request_id="reprice", parent_request=existing.request_id))
    snapshot = book.snapshot()
    snapshot["orders"]["aliased"] = snapshot["orders"].pop(existing.request_id)
    with pytest.raises(ContractError, match="snapshot key"):
        plan_new_forecast(snapshot, intent=intent("another", "another-forecast"), as_of=NOW, eligible_session=SESSION)


def test_complete_terminal_evidence_must_match_all_intervening_fills(tmp_path):
    book = ledger(tmp_path)
    submit_fixture(book, status="CANCELLED", fill_quantity=1)
    snapshot = book.snapshot()
    snapshot["evidence"]["status-old"]["fills"] = []
    with pytest.raises(ContractError, match="every intervening fill"):
        plan_new_forecast(snapshot, intent=intent(), as_of=NOW, eligible_session=SESSION)
    snapshot = book.snapshot()
    snapshot["orders"]["old"]["status"] = "CANCELLED"
    snapshot["evidence"]["status-old"]["complete"] = False
    with pytest.raises(ContractError, match="Complete status"):
        plan_new_forecast(snapshot, intent=intent(), as_of=NOW, eligible_session=SESSION)


def test_zero_quantity_new_intention_still_reconciles_old_commitment_without_quote(tmp_path):
    book = ledger(tmp_path)
    submit_fixture(book)
    zero = replace(intent(), desired=0, limit="0")
    result = plan(book, new=zero)
    assert result.state == "reconcile_required"
    assert result.prior_orders[0].action == "retrieve_status_then_cancel_unfilled"
    assert book.order("old")["status"] == "WORKING" and zero.desired == 0 and zero.limit == "0"
    book.cancel_requested("old")
    book.apply(OrderEvidence("zero-confirmed-cancel", "old", "broker-old", OBSERVED, "CANCELLED", 4, ()))
    assert plan(book, new=zero).state == "ready_for_other_checks"
    assert len(book.snapshot()["orders"]) == 1


def test_duplicate_fill_entries_cannot_stand_in_for_omitted_intervening_fill(tmp_path):
    book = ledger(tmp_path)
    old = intent("old", "forecast-old")
    book.reserve(old, admission_id="fixture")
    book.begin_submit("old")
    fills = (Fill("first", 1, "100", OBSERVED), Fill("second", 1, "100", OBSERVED))
    book.apply(OrderEvidence("cancelled", "old", "broker-old", OBSERVED, "CANCELLED", 4, fills))
    snapshot = book.snapshot()
    snapshot["evidence"]["cancelled"]["fills"] = [asdict(fills[0]), asdict(fills[0])]
    with pytest.raises(ContractError, match="every intervening fill"):
        plan_new_forecast(snapshot, intent=intent(), as_of=NOW, eligible_session=SESSION)


def test_scope_preserved_no_scout_activation_or_gameplan_replacement(tmp_path):
    book = ledger(tmp_path)
    for invalid in (replace(intent(), stream="scout-15m"), replace(intent(), stream="gameplan", horizon="1h"),
                    replace(intent(), symbol="ABCL"), replace(intent(), horizon="1h")):
        with pytest.raises(ContractError, match="scope"):
            plan(book, new=invalid)


def test_helper_has_no_live_side_effects_or_submission_imports():
    path = Path(__file__).parents[1]/"ml/intraday/execution_policy.py"
    tree = ast.parse(path.read_text())
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert set(modules) <= {"__future__", "collections.abc", "dataclasses", "datetime", "typing", "book", "prediction"}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  for node in [node.func]}
    assert not attributes & {"submit", "cancel", "reserve", "begin_submit", "cancel_requested", "apply", "connect", "open", "write_text"}
