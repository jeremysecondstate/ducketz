"""Record observed holdings for an explicitly expanded research universe.

This module has no order, reservation, allocation or trader-start operations.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_onboarding import _write


def prepare_ownership(root: Path, *, symbols: tuple[str, ...], plan_id: str,
                      output: Path, session=None, snapshot_loader=None) -> dict:
    from app.services.schwab import SchwabSession
    from ml.gameplan_trade_snapshot import capture_trade_planning_snapshot, _ownership
    from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence

    with exclusive_runtime_lock(root/'locks/stock-trader-hourly.lock', process_name='Research ownership preparation'):
        broker = session if session is not None else SchwabSession()
        snapshot = (snapshot_loader or capture_trade_planning_snapshot)(root, symbols=symbols, session=broker)
        if (snapshot.get('status') != 'OBSERVED' or snapshot.get('cash_status') != 'CASH_ONLY_BOUNDED'
                or set(snapshot.get('held_shares', {})) != set(symbols)):
            raise ValueError('Complete observed broker holdings are required for research onboarding')
        ownership = snapshot.get('ownership', {})
        result = {'plan_id':plan_id, 'symbols':list(symbols), 'observed_at':snapshot['observed_at'],
                  'status':'COMPLETE', 'orders_placed':0, 'broker_orders_enabled':False,
                  'recorded_observed_holdings':False}
        if ownership.get('safe_for_planning') is not True:
            if (ownership.get('reason_codes') != ['SAVED_RECONCILIATION_SYMBOL_MISSING']
                    or ownership.get('account_matches') is not True
                    or ownership.get('last_saved_reconciliation_ready') is not True
                    or snapshot.get('working_order_count') != 0):
                raise ValueError('Existing ownership or working-order discrepancies require native reconciliation')
            ledger_path = root/'state/independent-stock-trader/holdings.sqlite3'
            if not ledger_path.is_file():
                raise ValueError('Existing ownership ledger is required')
            identity = broker.stable_account_fingerprint()
            ledger = HorizonLedger(ledger_path, identity)
            state = ledger.snapshot()
            if state.persistent_blocks or any(r.status in {'RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'} for r in state.reservations):
                raise ValueError('Open ledger reservations or ownership blocks require native reconciliation')
            prices = {symbol:snapshot['quotes'][symbol]['price_reference'] for symbol in symbols}
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',',':')).encode()).hexdigest()
            _write(output.with_name('ownership-snapshot.json'), snapshot)
            evidence = PortfolioEvidence(
                hashlib.sha256((plan_id+digest).encode()).hexdigest(), identity, snapshot['observed_at'],
                snapshot['held_shares'], prices, {symbol:0 for symbol in symbols}, digest)
            reconciled = ledger.reconcile(evidence)
            if not reconciled.ready:
                raise ValueError('Observed holdings did not reconcile; existing ownership guards remain enforced')
            checked = _ownership(root, symbols, identity, snapshot['held_shares'], snapshot['observed_at'])
            if checked.get('safe_for_planning') is not True:
                raise ValueError('Recorded holdings did not pass the planning ownership check')
            result.update(recorded_observed_holdings=True, reconciliation=asdict(reconciled),
                          source_snapshot_sha256=digest, symbol_execution_budgets=0,
                          prior_snapshot_at=ownership.get('last_saved_reconciliation_at'))
        _write(output, result)
        return result
