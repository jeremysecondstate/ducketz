"""Read-only planning ownership over real, temporary directional-sale ledgers."""
from __future__ import annotations

import sqlite3

import pytest

from ml import gameplan_trade_snapshot as snapshot_reader
from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, OrderEvidence, PortfolioEvidence


ACCOUNT = "d" * 64
OPEN = "2026-09-09T11:00:00+00:00"
END = "2026-09-09T12:00:00+00:00"


def _portfolio(identity, *, at=OPEN, held=1):
    return PortfolioEvidence(identity, ACCOUNT, at, {"COST": held}, {"COST": 900},
        {"COST": 10000}, "e" * 64)


@pytest.fixture
def ledger(tmp_path):
    result = HorizonLedger(tmp_path / snapshot_reader._LEDGER_PATH, ACCOUNT)
    assert result.reconcile(_portfolio("starting-account")).ready
    return result


def _reserve(ledger, *, quantity=1):
    return ledger.reserve_direction_exit(symbol="COST", horizon="1h", forecast_id="frozen-bearish-COST",
        target_start=OPEN, target_end=END, quantity=quantity, limit_price=900,
        snapshot_id="starting-account", idempotency_key="direction-sale", batch_id="opening-hour",
        as_of="2026-09-09T11:00:10+00:00", pending_sell_shares=0)


def _order(reservation, *, status, filled=0, identity="broker-order-evidence", at="2026-09-09T11:00:20+00:00"):
    fills = (FillEvidence("confirmed-sale-fill", filled, 900, at),) if filled else ()
    remaining = 0 if status in {"FILLED", "CANCELLED", "REJECTED"} else reservation.quantity - filled
    return OrderEvidence(identity, reservation.reservation_id, ACCOUNT, at, "synthetic-broker-order",
        status, reservation.quantity, filled, remaining, fills)


def _read(tmp_path, ledger, *, held, observed="2026-09-09T11:00:40+00:00"):
    before = ledger.path.read_bytes()
    result = snapshot_reader._ownership(tmp_path, ("COST",), ACCOUNT, {"COST": held}, observed)
    assert ledger.path.read_bytes() == before
    assert result["account_matches"] is True
    assert result["current_broker_reconciliation_performed"] is False
    assert "UNEXPLAINED_SHARE_REDUCTION_SINCE_SAVED_RECONCILIATION" not in result["reason_codes"]
    return result


def test_old_ledger_without_optional_assignment_tables_still_reads(tmp_path, ledger):
    with sqlite3.connect(ledger.path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('inventory_assignments','inventory_assignment_releases')").fetchall()
    result = _read(tmp_path, ledger, held=1)
    assert result["safe_for_planning"] is True
    assert result["owned_shares"] == {"COST": 0}
    assert result["active_allocations"] == []


def test_reserved_existing_stock_is_owned_but_not_falsely_reported_as_a_fill(tmp_path, ledger):
    reserved = _reserve(ledger)
    result = _read(tmp_path, ledger, held=1)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason_codes"] == ["PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION"]
    assert result["owned_shares"] == {"COST": 1}
    assert len(result["active_allocations"]) == 1
    assert result["active_allocations"][0]["owned_shares"] == 1
    assert result["active_allocations"][0]["reserved_sell_shares"] == 1
    assert ledger.snapshot().reservations[0].filled_quantity == 0
    assert reserved.side == "SELL"
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_confirmed_sale_zeroes_assigned_stock_and_planning_ownership(tmp_path, ledger):
    reserved = _reserve(ledger)
    assert ledger.reconcile(_portfolio("confirmed-sale-account", at="2026-09-09T11:00:21+00:00", held=0),
        order_evidence=(_order(reserved, status="FILLED", filled=1),)).ready
    result = _read(tmp_path, ledger, held=0)
    assert result["status"] == "OBSERVED_CONSISTENT"
    assert result["safe_for_planning"] is True
    assert result["owned_shares"] == {"COST": 0}
    assert result["active_allocations"] == []
    assert result["reason_codes"] == []
    assert ledger.snapshot().reservations[0].filled_quantity == 1


def test_definitive_rejection_returns_unsold_stock_without_a_new_snapshot_or_false_reduction(tmp_path, ledger):
    reserved = _reserve(ledger)
    ledger.mark_submission(reserved.reservation_id, status="REJECTED", evidence_id="definitive-rejection",
        observed_at="2026-09-09T11:00:20+00:00")
    result = _read(tmp_path, ledger, held=1)
    assert result["safe_for_planning"] is True
    assert result["owned_shares"] == {"COST": 0}
    assert result["active_allocations"] == []
    assert result["last_saved_reconciliation_at"] == OPEN
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT quantity FROM inventory_assignment_releases").fetchall() == [(1,)]
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


@pytest.mark.parametrize("filled", [0, 1])
def test_cancel_releases_only_unsold_assignment_after_an_intermediate_ready_baseline(tmp_path, filled):
    ledger = HorizonLedger(tmp_path / snapshot_reader._LEDGER_PATH, ACCOUNT)
    assert ledger.reconcile(_portfolio("starting-account", held=2)).ready
    reserved = _reserve(ledger, quantity=2)
    assert ledger.reconcile(_portfolio("working-account", at="2026-09-09T11:00:21+00:00", held=2),
        order_evidence=(_order(reserved, status="WORKING"),)).ready
    cancelled = _order(reserved, status="CANCELLED", filled=filled, identity="cancelled-order-evidence",
        at="2026-09-09T11:00:30+00:00")
    assert ledger.reconcile(_portfolio("cancelled-account", at="2026-09-09T11:00:31+00:00", held=2-filled),
        order_evidence=(cancelled,)).ready
    result = _read(tmp_path, ledger, held=2-filled)
    assert result["safe_for_planning"] is True
    assert result["owned_shares"] == {"COST": 0}
    assert result["active_allocations"] == []
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT snapshot_id,quantity FROM inventory_assignment_releases").fetchall() == [("working-account", 2-filled)]
        assert db.execute("SELECT COALESCE(SUM(quantity),0) FROM fills").fetchone()[0] == filled


def test_rejected_sale_release_after_saved_working_state_is_not_a_manual_share_change(tmp_path, ledger):
    reserved = _reserve(ledger)
    assert ledger.reconcile(_portfolio("working-account", at="2026-09-09T11:00:21+00:00"),
        order_evidence=(_order(reserved, status="WORKING"),)).ready
    # Apply broker terminal evidence through the real API and its new ready
    # portfolio; the next read must not mistake assignment release for a sale.
    assert ledger.reconcile(_portfolio("rejected-account", at="2026-09-09T11:00:31+00:00"),
        order_evidence=(_order(reserved, status="REJECTED", identity="terminal-rejection", at="2026-09-09T11:00:30+00:00"),)).ready
    result = _read(tmp_path, ledger, held=1)
    assert result["safe_for_planning"] is True
    assert result["owned_shares"] == {"COST": 0}
    assert result["reason_codes"] == []
