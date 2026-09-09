from dataclasses import replace
import hashlib

import pytest

from app.services.schwab import SchwabOrderSubmissionContext, SchwabSession
from ml.stock_trader.horizon_ledger import HorizonLedger, LedgerError
from test_stock_horizon_ledger import entry, portfolio


@pytest.fixture
def session(monkeypatch):
    value = object.__new__(SchwabSession)
    value.access_token = "fake-access-generation-1"
    value.refresh_token = "fake-refresh-generation-1"
    value.account_hash = "fake-account-alpha"
    monkeypatch.setattr(value, "ensure_access_token", lambda: None)
    monkeypatch.setattr(value, "_get_account_hash_for_current_token", lambda: value.account_hash)
    return value


def test_durable_inventory_identity_survives_oauth_refresh(session, tmp_path):
    stable = session.stable_account_fingerprint()
    transport = session.prepare_read_snapshot()
    assert stable == hashlib.sha256(b"ducketz-stock-inventory-v1\0fake-account-alpha").hexdigest()
    assert len(stable) == 64 and "fake-account-alpha" not in stable
    ledger = HorizonLedger(tmp_path / "inventory.sqlite3", stable)
    assert ledger.reconcile(replace(portfolio(), account_fingerprint=stable)).ready
    reservation = entry(ledger)

    session.access_token = "fake-access-generation-2"
    session.refresh_token = "fake-refresh-generation-2"
    assert session.stable_account_fingerprint() == stable
    assert session.prepare_read_snapshot() != transport
    with pytest.raises(RuntimeError, match="identity or authorization changed"):
        session.verify_read_snapshot(transport)
    reopened = HorizonLedger(ledger.path, session.stable_account_fingerprint())
    assert reopened.lookup_reservation(reservation.reservation_id) == reservation


def test_actual_account_change_cannot_reuse_inventory(session, tmp_path):
    original = session.stable_account_fingerprint()
    ledger = HorizonLedger(tmp_path / "inventory.sqlite3", original)
    session.account_hash = "fake-account-beta"
    changed = session.stable_account_fingerprint()
    assert changed != original
    with pytest.raises(LedgerError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        HorizonLedger(ledger.path, changed)


def test_prepared_cancellation_uses_frozen_context_and_calls_last_gate(session, monkeypatch):
    context = session.prepare_order_submission()
    calls = []

    class Response:
        def raise_for_status(self):
            calls.append("checked-response")

    def fake_delete(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("app.services.schwab.requests.delete", fake_delete)
    # The context must not silently switch accounts or tokens after preparation.
    session.account_hash = "fake-account-beta"
    session.access_token = "fake-access-generation-2"
    session.cancel_prepared_order(" 12345 ", context, before_delete=lambda: calls.append("last-gate"))
    assert calls == ["last-gate", (
        "https://api.schwabapi.com/trader/v1/accounts/fake-account-alpha/orders/12345",
        {"headers": {"Authorization": "Bearer fake-access-generation-1"}, "timeout": 10},
    ), "checked-response"]


def test_failed_last_gate_prevents_cancellation_request(session, monkeypatch):
    context = session.prepare_order_submission()
    calls = []
    monkeypatch.setattr("app.services.schwab.requests.delete", lambda *a, **kw: calls.append("delete"))

    def revoked():
        raise RuntimeError("operator disabled trading")

    with pytest.raises(RuntimeError, match="operator disabled"):
        session.cancel_prepared_order("12345", context, before_delete=revoked)
    assert calls == []


@pytest.mark.parametrize("order_id", ["", "../other-account", "123/456", "abc", "1?account=other"])
def test_cancellation_requires_an_exact_numeric_tracked_order(session, monkeypatch, order_id):
    context = session.prepare_order_submission()
    calls = []
    monkeypatch.setattr("app.services.schwab.requests.delete", lambda *a, **kw: calls.append("delete"))
    with pytest.raises(ValueError, match="numeric tracked"):
        session.cancel_prepared_order(order_id, context, before_delete=lambda: calls.append("gate"))
    assert calls == []


def test_cancel_failure_is_returned_to_caller_without_retry(session, monkeypatch):
    context = session.prepare_order_submission()
    calls = []

    def timeout(*args, **kwargs):
        calls.append("delete")
        raise TimeoutError("unknown request result")

    monkeypatch.setattr("app.services.schwab.requests.delete", timeout)
    with pytest.raises(TimeoutError, match="unknown request result"):
        session.cancel_prepared_order("12345", context)
    assert calls == ["delete"]


def test_stale_transport_identity_blocks_before_delete_after_oauth_change(session, monkeypatch):
    transport = session.prepare_read_snapshot()
    context = session.prepare_order_submission()
    session.access_token = "fake-access-generation-2"
    session.refresh_token = "fake-refresh-generation-2"
    calls = []
    monkeypatch.setattr("app.services.schwab.requests.delete", lambda *a, **kw: calls.append("delete"))
    with pytest.raises(RuntimeError, match="identity or authorization changed"):
        session.cancel_prepared_order("12345", context,
                                      before_delete=lambda: session.verify_read_snapshot(transport))
    assert not calls


def test_prepared_cancel_rejects_untyped_context_before_safety_callback(session, monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.schwab.requests.delete", lambda *a, **kw: calls.append("delete"))
    with pytest.raises(TypeError, match="context"):
        session.cancel_prepared_order("12345", object(), before_delete=lambda: calls.append("gate"))
    assert not calls
