"""Bounded native reconciliation of two verified, unfilled cancelled orders.

Artifact-only operator helper. No control writes, trader starts, allocation or
reservation creation, order actions, direct ledger SQL writes, or lock removal.
Root supervisor must keep its existing lease renewed while this helper runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")

import psutil
from app.services.schwab import SchwabSession
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS, canonical_sha256, utc
from ml.stock_trader.control import operator_intent_path
from ml.stock_trader.gameplan import gameplan_stock_activation_path
from ml.stock_trader.horizon_broker import capture_order_evidence
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.stock_trader.runtime import _capture_portfolio_state_with_retry
from ml.stock_trader.state import capture_portfolio_state

ROOT = Path("C:/DATASTORE")
OUT = Path(__file__).resolve().parent
TOKEN = "07cc0897-098e-4dab-9efb-6ff13ab395e3"
ACCOUNT = "85dc27ef421228b44aa818e0b541306fa5d6cc5cc7448a2bc5a09f99f746258c"
DB = ROOT / "state/independent-stock-trader/holdings.sqlite3"
STATUS = ROOT / "state/independent-stock-trader/session-status.json"
SESSION_LOCK = ROOT / "locks/independent-stock-session.lock"
CYCLE_LOCK = ROOT / "locks/stock-trader-hourly.lock"
EXPECTED = {
    "8755dd3b6ec6379052315568b53519e4dcdc8ccd6bb3e290991deac9e598461a":
        ("CROX", "1h", "SELL", 31, "1008007537441", Decimal("123.87")),
    "488fcc7ae18dad004f2ad6cdb98464f4ed2fda505223f19192ac7af167262474":
        ("IONQ", "1h", "BUY", 48, "1008008404182", Decimal("40.6")),
}


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_owner():
    lease = read(ROOT / "ml/overnight-supervision.json")
    require(lease.get("owner_token") == TOKEN, "SUPERVISION_OWNER_CHANGED")
    require(utc(lease["expires_at"]) > utc(), "SUPERVISION_LEASE_EXPIRED")


def guarded_artifacts():
    paths = [STATUS, gameplan_stock_activation_path(ROOT), operator_intent_path(ROOT)]
    for folder in ("entry-slots", "quote-recovery-slots"):
        paths.extend(p for p in (ROOT / "state/independent-stock-trader" / folder).rglob("*") if p.is_file())
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in paths}


def check_finished_session():
    status = read(STATUS)
    require(status.get("status") == "FINISHED", "TRADER_SESSION_NOT_FINISHED")
    require(status.get("action_date") == "2026-09-21", "TRADER_SESSION_DATE_CHANGED")
    require(status.get("pid") == 42576, "TRADER_SESSION_ID_CHANGED")
    require(status.get("started_at") == "2026-09-21T12:18:05.386720+00:00", "TRADER_SESSION_START_CHANGED")
    require(status.get("sizing_policy") == "gameplan-direction-current-market-v1", "TRADER_POLICY_CHANGED")
    require(status.get("wait_for_open") is True and status.get("late_opening_date") is None,
            "TRADER_SESSION_OPTIONS_CHANGED")
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = process.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        require(not any(arg in {"ml.gameplan_stock_trader", "ml.stock_trader.runtime"} for arg in command),
                "LIVE_STOCK_TRADER_PROCESS_PRESENT")
    require(not psutil.pid_exists(42576), "PRIOR_TRADER_PID_PRESENT")
    return status


def check_pending(ledger):
    pending = ledger.pending_reservations()
    require({r.reservation_id for r in pending} == set(EXPECTED), "PENDING_RESERVATION_SET_CHANGED")
    for row in pending:
        expected = EXPECTED[row.reservation_id]
        actual = (row.symbol, row.horizon, row.side, row.quantity,
                  row.broker_order_id, Decimal(str(row.limit_price)))
        require(actual == expected, "PENDING_RESERVATION_IDENTITY_CHANGED")
        require(row.status == "WORKING" and row.filled_quantity == 0, "PENDING_RESERVATION_STATE_CHANGED")
        require(row.cancel_requested_at is None, "NEW_CANCELLATION_CLAIM_PRESENT")
    return pending


class ReadOnlyBroker:
    """Expose only the native read surface to evidence capture helpers."""
    def __init__(self):
        self._session = SchwabSession()

    def prepare_read_snapshot(self):
        return self._session.prepare_read_snapshot()

    def verify_read_snapshot(self, identity):
        return self._session.verify_read_snapshot(identity)

    def stable_account_fingerprint(self):
        return self._session.stable_account_fingerprint()

    def get_orders(self, **kwargs):
        return self._session.get_orders(**kwargs)

    def get_account(self):
        return self._session.get_account()

    def get_open_orders(self):
        return self._session.get_open_orders()

    def get_equity_quotes(self, symbols):
        return self._session.get_equity_quotes(symbols)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="Apply only the two guarded native terminal order reconciliations")
    parser.parse_args()
    receipt_path = OUT / "native-reconciliation.json"
    backup_path = OUT / "holdings-before-reconciliation.sqlite3"
    require(not receipt_path.exists() and not backup_path.exists(), "RECONCILIATION_ARTIFACT_ALREADY_EXISTS")
    record = {"started_at": utc().isoformat(), "supervision_owner": TOKEN,
              "status": "PREFLIGHT", "native_reconcile_called": False,
              "orders_submitted": 0, "orders_cancelled": 0, "orders_replaced": 0,
              "allocations_created": 0, "reservations_created": 0,
              "broker_data_http_methods": ["GET"], "execution_budgets": "ZERO"}
    try:
        check_owner()
        terminal = check_finished_session()
        require(not SESSION_LOCK.exists() and not CYCLE_LOCK.exists(), "EXISTING_TRADER_LOCK_PRESERVED")
        guarded = guarded_artifacts()
        # Native lock order matches session -> hourly runtime. Refuse preexisting
        # locks instead of asking the lock helper to recover any stale owner.
        with exclusive_runtime_lock(SESSION_LOCK, process_name="terminal-order-reconciliation-session"):
            require(not CYCLE_LOCK.exists(), "EXISTING_CYCLE_LOCK_PRESERVED")
            with exclusive_runtime_lock(CYCLE_LOCK, process_name="terminal-order-reconciliation"):
                check_owner()
                check_finished_session()
                require(guarded_artifacts() == guarded, "GUARDED_ARTIFACT_CHANGED")
                with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as source:
                    source.row_factory = sqlite3.Row
                    source.execute("PRAGMA query_only=ON")
                    require(dict(source.execute("SELECT key,value FROM metadata")).get("account") == ACCOUNT,
                            "SAVED_LEDGER_ACCOUNT_CHANGED")
                    latest = dict(source.execute("SELECT * FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone())
                    require(latest["ready"] == 1 and json.loads(latest["reasons"]) == [], "PRIOR_RECONCILIATION_NOT_READY")
                    # This zero-fill recovery must not release any separately
                    # assigned inventory or change any filled allocation.
                    for reservation_id in EXPECTED:
                        count = source.execute("""SELECT count(*) FROM inventory_assignments i
                            JOIN reservations r ON r.allocation=i.allocation WHERE r.id=?""",
                            (reservation_id,)).fetchone()[0]
                        require(count == 0, "TARGET_HAS_ASSIGNED_INVENTORY")
                    with sqlite3.connect(backup_path) as backup:
                        source.backup(backup)
                record.update(backup_path=str(backup_path), backup_sha256=sha(backup_path),
                              terminal_status=terminal, guarded_artifact_sha256=guarded)
                broker = ReadOnlyBroker()
                auth_identity = broker.prepare_read_snapshot()
                require(broker.stable_account_fingerprint() == ACCOUNT, "BROKER_ACCOUNT_MISMATCH")
                ledger = HorizonLedger(DB, ACCOUNT)
                original = ledger.snapshot()
                require(not original.persistent_blocks, "PERSISTENT_OWNERSHIP_BLOCKS")
                check_pending(ledger)
                captured = {}

                def capture_snapshot(_timestamp):
                    check_owner()
                    evidence = capture_order_evidence(broker, ledger, account_fingerprint=ACCOUNT,
                                                       as_of=utc(), observation_clock=utc)
                    require({e.reservation_id for e in evidence} == set(EXPECTED), "ORDER_EVIDENCE_SET_CHANGED")
                    for item in evidence:
                        require(item.status == "CANCELLED" and item.broker_status in {"CANCELED", "CANCELLED"},
                                "ORDER_NOT_VERIFIED_CANCELLED")
                        require(item.account_fingerprint == ACCOUNT and item.broker_order_id == EXPECTED[item.reservation_id][4],
                                "ORDER_EVIDENCE_IDENTITY_CHANGED")
                        require(item.order_quantity == EXPECTED[item.reservation_id][3]
                                and item.cumulative_filled_quantity == 0 and item.remaining_quantity == 0
                                and not item.fills, "ORDER_ZERO_FILL_TERMINAL_STATE_CHANGED")
                    portfolio = capture_portfolio_state(broker, observed_at=utc(), parallel=True,
                                                       literal_cash_only=True, use_actual_quote_timestamps=True)
                    captured["evidence"] = evidence
                    return portfolio

                portfolio, _, metadata = _capture_portfolio_state_with_retry(broker, observed_at=utc(),
                    parallel=True, retry_delay_seconds=3, maximum_retry_seconds=120, maximum_attempts=40,
                    sleep=time.sleep, monotonic=time.monotonic, capture_snapshot=capture_snapshot)
                evidence = captured["evidence"]
                broker.verify_read_snapshot(auth_identity)
                broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
                require(broker.stable_account_fingerprint() == ACCOUNT, "BROKER_ACCOUNT_CHANGED")
                require(portfolio.working_order_count == 0, "BROKER_WORKING_ORDERS_PRESENT")
                require(not any(portfolio.pending_buy_shares.values()) and not any(portfolio.pending_sell_shares.values()),
                        "BROKER_PENDING_SHARES_PRESENT")
                require(set(portfolio.held_shares) == set(STOCK_TRADER_SYMBOLS)
                        and set(portfolio.quotes) == set(STOCK_TRADER_SYMBOLS), "INCOMPLETE_PORTFOLIO_SYMBOLS")
                require(0 <= (utc() - utc(portfolio.observed_at)).total_seconds() <= ledger.maximum_evidence_age_seconds,
                        "PORTFOLIO_EVIDENCE_NOT_FRESH")
                require(all(utc(e.observed_at) < utc(portfolio.observed_at) for e in evidence),
                        "PORTFOLIO_MUST_FOLLOW_ORDER_EVIDENCE")
                previous_held = json.loads(latest["payload"])["held_shares"]
                require({s: Decimal(str(v)) for s, v in portfolio.held_shares.items()}
                        == {s: Decimal(str(v)) for s, v in previous_held.items()}, "HELD_SHARES_CHANGED_SINCE_TERMINAL_SESSION")
                check_owner()
                check_finished_session()
                require(guarded_artifacts() == guarded, "GUARDED_ARTIFACT_CHANGED")
                require(ledger.snapshot() == original, "LEDGER_CHANGED_DURING_CAPTURE")
                check_pending(ledger)
                record.update(before=asdict(original), order_evidence=[asdict(e) for e in evidence],
                              portfolio=asdict(portfolio), broker_state_capture=metadata,
                              native_reconcile_called=True)
                result = ledger.reconcile(PortfolioEvidence(
                    canonical_sha256([ACCOUNT, utc().isoformat(), portfolio.source_fingerprint]),
                    ACCOUNT, portfolio.observed_at, portfolio.held_shares,
                    {s: q.ask for s, q in portfolio.quotes.items()},
                    {s: 0 for s in portfolio.held_shares}, portfolio.source_fingerprint), order_evidence=evidence)
                after = ledger.snapshot()
                record.update(result=asdict(result), after=asdict(after))
                require(result.ready and not result.reasons, "NATIVE_RECONCILIATION_NOT_READY")
                require(not ledger.pending_reservations() and not after.persistent_blocks, "UNRESOLVED_LEDGER_STATE")
                require({a.allocation_id: a.filled_shares for a in original.allocations}
                        == {a.allocation_id: a.filled_shares for a in after.allocations}, "FILLED_ALLOCATIONS_CHANGED")
                require({r.reservation_id for r in original.reservations}
                        == {r.reservation_id for r in after.reservations}, "RESERVATION_IDENTITIES_CHANGED")
                before_other = {r.reservation_id: r for r in original.reservations if r.reservation_id not in EXPECTED}
                after_other = {r.reservation_id: r for r in after.reservations if r.reservation_id not in EXPECTED}
                require(before_other == after_other, "UNRELATED_RESERVATION_CHANGED")
                require(guarded_artifacts() == guarded, "GUARDED_ARTIFACT_CHANGED")
                record.update(status="RECONCILED", pending_reservations=0, filled_allocations_preserved=True,
                              controls_session_and_entry_claims_preserved=True)
    except Exception as exc:
        record.update(status="FAILED", error_type=type(exc).__name__,
                      error_code=str(exc) if str(exc).replace("_", "").isalnum() and str(exc).isupper() else "INSPECT_CAPTURE_FAILURE")
        raise
    finally:
        record["finished_at"] = utc().isoformat()
        with receipt_path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, default=str)
            handle.write("\n")
        print(json.dumps({k: record[k] for k in ("status", "native_reconcile_called", "finished_at")}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # The structured receipt contains a sanitized error. Avoid printing a
        # provider exception's request URL or other authentication context.
        raise SystemExit(1) from None
