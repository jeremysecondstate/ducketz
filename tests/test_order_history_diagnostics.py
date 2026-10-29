"""Offline exact-order omissions retain private identities without changing decisions.

The real capture/normalizer and pure ledger types are loaded from candidate
source. Only the serializer is AST-extracted to avoid importing a live adapter.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "a" * 64
OBSERVED = "2026-10-29T11:18:27+00:00"
COMPLETED = "2026-10-29T11:18:30+00:00"
REASON = "TRACKED_ORDER_MISSING_FROM_BOUNDED_HISTORY"


@pytest.fixture
def broker():
    path = ROOT / "ml/stock_trader/horizon_ledger.py"
    name = "_missing_history_real_ledger"
    spec = importlib.util.spec_from_file_location(name, path)
    ledger = importlib.util.module_from_spec(spec)
    sys.modules[name] = ledger
    spec.loader.exec_module(ledger)
    namespace = {"__name__": "_missing_history_real_capture"}
    names = ("FillEvidence", "HorizonLedger", "LedgerError", "OrderEvidence",
             "ReservationState", "_utc", "validate_fill_time_provenance")
    namespace.update({key: getattr(ledger, key) for key in names})
    source = ROOT / "ml/stock_trader/horizon_broker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module == "ml.stock_trader.horizon_ledger")]
    exec(compile(tree, str(source), "exec"), namespace)
    yield SimpleNamespace(**namespace)
    sys.modules.pop(name, None)


def reservation(identity="first-missing", *, symbol="NVDA", broker_id="tracked-1", status="SUBMITTED"):
    return SimpleNamespace(reservation_id=identity, broker_order_id=broker_id,
                           symbol=symbol, side="BUY", status=status, quantity=3)


class Ledger:
    account_fingerprint = ACCOUNT

    def __init__(self, pending):
        self.pending = tuple(pending)

    def pending_reservations(self):
        return self.pending


class Session:
    def __init__(self, raw):
        self.raw, self.calls, self.identity_reads = raw, [], 0

    def stable_account_fingerprint(self):
        self.identity_reads += 1
        return ACCOUNT

    def get_orders(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw


def omitted(broker, *, pending=None, raw=None, clock=lambda: COMPLETED):
    ledger = Ledger(pending if pending is not None else [reservation()])
    session = Session(raw if raw is not None else [])
    before = deepcopy(vars(ledger))
    with pytest.raises(broker.OrderHistoryError, match="^" + REASON + "$") as caught:
        broker.capture_order_evidence(session, ledger, account_fingerprint=ACCOUNT,
                                     as_of=OBSERVED, observation_clock=clock)
    assert vars(ledger) == before
    assert len(session.calls) == 1 and session.identity_reads == 2
    assert str(caught.value) == REASON
    return caught.value, session


def serializer(broker):
    source = ROOT / "ml/intraday/account_execution.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = {"_SAFE_SUBMISSION_REASONS", "_AUTHORITY_RENEWAL_FAILURE", "_SQLITE_ERROR_CONSTANTS"}
    selected = [node for node in tree.body if (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names
                                            for target in node.targets)) or (
        isinstance(node, ast.FunctionDef) and node.name == "_submission_failure_evidence")]
    assert len(selected) == 4
    namespace = dict(sqlite3=sqlite3, ContractError=type("ContractError", (ValueError,), {}),
                     AuthorityError=type("AuthorityError", (ValueError,), {}),
                     lease_acquisition_failure_evidence=lambda error: None,
                     order_history_failure_evidence=broker.order_history_failure_evidence)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_exact_first_missing_identity_request_bounds_and_counts_are_saved(broker):
    first = reservation(symbol="NVDA", status="PARTIAL")
    raw = [{"orderId": "unrelated", "privateProviderText": "do-not-copy",
            "childOrderStrategies": [{"orderId": "unrelated-child"}]}]
    error, session = omitted(broker, pending=[first, reservation("second", broker_id="tracked-2")], raw=raw)
    details = broker.order_history_failure_evidence(error)
    assert details == {
        "schema_version": "bounded-order-history-missing-v1",
        "reservation_id": "first-missing", "broker_order_id": "tracked-1", "symbol": "NVDA",
        "side": "BUY", "reservation_status": "PARTIAL", "identity_fields_unavailable": False,
        "query_from_entered_time": "2026-10-15T11:18:27+00:00",
        "query_to_entered_time": OBSERVED, "observed_at": COMPLETED,
        "observation_clock_used": True, "max_results": broker.ORDER_HISTORY_LIMIT,
        "response_count": 1, "walked_unique_id_count": 2, "pending_count": 2,
    }
    assert session.calls[0] == {
        "from_entered_time": datetime(2026, 10, 15, 11, 18, 27, tzinfo=timezone.utc),
        "to_entered_time": datetime(2026, 10, 29, 11, 18, 27, tzinfo=timezone.utc),
        "max_results": broker.ORDER_HISTORY_LIMIT,
    }
    assert "do-not-copy" not in json.dumps(details) and ACCOUNT not in json.dumps(details)


def test_missing_details_return_defensive_scalar_copies(broker):
    error, _ = omitted(broker)
    details = broker.order_history_failure_evidence(error)
    details["symbol"] = "PATH"
    details["extra"] = "injected"
    assert broker.order_history_failure_evidence(error)["symbol"] == "NVDA"
    assert "extra" not in broker.order_history_failure_evidence(error)
    assert all(type(value) in {str, int, bool, type(None)} for value in details.values())


def test_no_observation_clock_is_reported_without_inventing_completion(broker):
    error, _ = omitted(broker, clock=None)
    details = broker.order_history_failure_evidence(error)
    assert details["observed_at"] == OBSERVED and details["observation_clock_used"] is False


@pytest.mark.parametrize("value", ["a" * 257, "tracked\nprivate", "token?secret", 7, None])
def test_unsafe_identity_values_are_withheld_without_truncation_or_coercion(broker, value):
    error, _ = omitted(broker, pending=[reservation(broker_id=value if value is not None else "tracked-1",
                                                 identity=value)])
    details = broker.order_history_failure_evidence(error)
    assert details["reservation_id"] is None and details["identity_fields_unavailable"] is True
    if value is not None:
        assert details["broker_order_id"] is None
    assert len(json.dumps(details)) < 2000


def test_diagnostic_creation_failure_preserves_fixed_error_and_single_request(broker, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-diagnostic-failure")
    monkeypatch.setitem(broker.capture_order_evidence.__globals__, "_remember_missing_order_history", fail)
    error, _ = omitted(broker)
    assert broker.order_history_failure_evidence(error) is None
    assert "private-diagnostic" not in str(error)


def test_plain_forged_or_borrowed_marker_cannot_supply_details(broker):
    forged = broker.OrderHistoryError(REASON)
    forged._missing_order_history_details = {"symbol": "private-forgery"}
    assert broker.order_history_failure_evidence(forged) is None
    real, _ = omitted(broker)
    forged._missing_order_history_details = real._missing_order_history_details
    assert broker.order_history_failure_evidence(forged) is None
    other = ValueError(REASON)
    other._missing_order_history_details = real._missing_order_history_details
    assert broker.order_history_failure_evidence(other) is None


@pytest.mark.parametrize("raw,reason", [({}, "COMPLETE_LIST"),
    ([{"orderId": "duplicate"}, {"orderId": "duplicate"}], "DUPLICATE_ORDER_IDS"),
    ([{}], "ROW_MISSING_ID"), ([{"orderId": "many"}] * 3000, "TRUNCATED")])
def test_other_fail_closed_history_errors_keep_their_original_reason(broker, raw, reason):
    session = Session(raw)
    with pytest.raises(broker.OrderHistoryError, match=reason) as caught:
        broker.capture_order_evidence(session, Ledger([reservation()]),
                                     account_fingerprint=ACCOUNT, as_of=OBSERVED)
    assert broker.order_history_failure_evidence(caught.value) is None
    assert len(session.calls) == 1


def test_unknown_without_exact_id_never_reads_history_or_creates_omission(broker):
    session = Session([])
    with pytest.raises(broker.OrderHistoryError, match="WITHOUT_EXACT") as caught:
        broker.capture_order_evidence(session, Ledger([reservation(broker_id=None)]),
                                     account_fingerprint=ACCOUNT, as_of=OBSERVED)
    assert not session.calls and session.identity_reads == 0
    assert broker.order_history_failure_evidence(caught.value) is None


def working_order():
    return {"orderId": "tracked-1", "status": "WORKING", "quantity": 3,
        "filledQuantity": 0, "remainingQuantity": 3,
        "orderLegCollection": [{"legId": 1, "instruction": "BUY", "quantity": 3,
            "instrument": {"assetType": "EQUITY", "symbol": "NVDA"}}],
        "orderActivityCollection": []}


def test_complete_exact_history_keeps_normalized_evidence_without_mutating_ledger(broker):
    ledger = Ledger([reservation()])
    before = deepcopy(vars(ledger))
    session = Session([working_order()])
    result = broker.capture_order_evidence(session, ledger, account_fingerprint=ACCOUNT,
                                          as_of=OBSERVED, observation_clock=lambda: COMPLETED)
    assert len(result) == 1 and len(session.calls) == 1
    assert result[0].reservation_id == "first-missing" and result[0].broker_order_id == "tracked-1"
    assert result[0].status == "WORKING" and result[0].cumulative_filled_quantity == 0
    assert result[0].observed_at == COMPLETED and vars(ledger) == before


def test_partial_normalization_is_never_returned_after_a_later_omission(broker):
    error, _ = omitted(broker, pending=[reservation(), reservation("omitted-second", broker_id="tracked-2")],
                       raw=[working_order()])
    details = broker.order_history_failure_evidence(error)
    assert details["reservation_id"] == "omitted-second" and details["broker_order_id"] == "tracked-2"
    assert details["pending_count"] == 2 and details["walked_unique_id_count"] == 1


def test_serializer_preserves_exact_omitted_symbol_even_for_another_trigger(broker):
    # PATH may trigger this pass while an NVDA order is missing from the own book.
    error, _ = omitted(broker, pending=[reservation(symbol="NVDA")])
    result = serializer(broker)["_submission_failure_evidence"](error, "order_evidence_capture")
    assert result["root_cause"]["order_history"]["symbol"] == "NVDA"
    assert result["root_cause"]["order_history"]["reservation_id"] == "first-missing"
    assert result["submission_stage"] == "order_evidence_capture"
    assert "PATH" not in json.dumps(result)


def test_serializer_preserves_details_in_real_exception_cause_chain(broker):
    error, _ = omitted(broker)
    try:
        raise RuntimeError("private-provider-message") from error
    except RuntimeError as wrapped:
        result = serializer(broker)["_submission_failure_evidence"](wrapped, "order_evidence_capture")
    assert len(result["exception_chain"]) == 2
    assert result["root_cause"]["order_history"]["broker_order_id"] == "tracked-1"
    assert "private-provider-message" not in json.dumps(result)


def test_serializer_ignores_forged_details_and_optional_reader_failure(broker):
    forged = broker.OrderHistoryError(REASON)
    forged._missing_order_history_details = {"private": "forgery"}
    namespace = serializer(broker)
    first = namespace["_submission_failure_evidence"](forged, "order_evidence_capture")
    assert "order_history" not in first["root_cause"]
    def fail(_error):
        raise RuntimeError("private-reader-failure")
    namespace["order_history_failure_evidence"] = fail
    second = namespace["_submission_failure_evidence"](forged, "order_evidence_capture")
    assert "order_history" not in second["root_cause"]
    assert "private" not in json.dumps(second)
