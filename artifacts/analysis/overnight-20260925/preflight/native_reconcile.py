"""Source-bound post-close native reconciliation; root must review before --apply.

GET-only broker facade. Exact pending identities, terminal order evidence, newer
coherent holdings, session/cycle locks, immutable backup, and native copy rehearsal
all precede the one production reconcile. No order/cancel/replace API is exposed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, 'C:/dev/ducketz')

import psutil
from app.services.schwab import SchwabSession
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.gameplan_trade_snapshot import _ownership
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS, canonical_sha256, utc
from ml.stock_trader.horizon_broker import capture_order_evidence
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence, _identity
from ml.stock_trader.runtime import _capture_portfolio_state_with_retry
from ml.stock_trader.state import capture_portfolio_state

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
DB = ROOT/'state/independent-stock-trader/holdings.sqlite3'
STATUS = ROOT/'state/independent-stock-trader/session-status.json'
SESSION_LOCK = ROOT/'locks/independent-stock-session.lock'
CYCLE_LOCK = ROOT/'locks/stock-trader-hourly.lock'
OPEN = {'RESERVED', 'SUBMITTED', 'UNKNOWN', 'WORKING', 'PARTIAL'}
TERMINAL = {'FILLED', 'CANCELLED', 'REJECTED'}


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, default=str)
        handle.write('\n')


def connect(path):
    require(not Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists(), 'READ_ONLY_WAL_STATE_UNAVAILABLE')
    connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    return connection


def table_state(path):
    # Bounded local ledger; no market-archive scan. Values are never printed.
    require(path.stat().st_size < 256*1024*1024, 'LEDGER_AUDIT_SIZE_BOUND_EXCEEDED')
    with connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: [dict(row) for row in connection.execute('SELECT * FROM "'+name+'" ORDER BY rowid')]
                for name in names}


def state_hash(state):
    return canonical_sha256(state)


def check_owner(plan):
    lease = read(ROOT/'ml/overnight-supervision.json')
    require(lease.get('owner_token') == plan['supervision_owner'], 'SUPERVISION_OWNER_CHANGED')
    require(utc(lease['expires_at']) > utc(), 'SUPERVISION_LEASE_EXPIRED')


def check_terminal(plan):
    require(sha(STATUS) == plan['terminal_status_sha256'], 'TERMINAL_STATUS_CHANGED')
    saved = read(STATUS)
    require(saved['status'] in {'FINISHED', 'FINISHED_WITH_ERRORS'}, 'TRADER_NOT_TERMINAL')
    require(saved['action_date'] == plan['source_date'], 'TERMINAL_SOURCE_DATE_CHANGED')
    require(saved['last_cycle']['selected_orders'] == 0 and saved['last_cycle']['submitted_orders'] == 0,
            'TERMINAL_CYCLE_HAS_ORDER_ACTIONS')
    for process in psutil.process_iter(['pid','cmdline']):
        try:
            command = process.info['cmdline'] or []
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        require(not any(arg in {'ml.gameplan_stock_trader','ml.stock_trader.runtime'} for arg in command),
                'LIVE_OR_WAITING_TRADER_PRESENT')
    require({path: sha(Path(path)) for path in plan['guarded_artifact_sha256']}
            == plan['guarded_artifact_sha256'], 'GUARDED_ARTIFACT_CHANGED')
    # Include newly created entry claims, not only files known at preparation.
    current_claims = sorted(str(p) for folder in ('entry-slots','quote-recovery-slots')
                           for p in (ROOT/'state/independent-stock-trader'/folder).rglob('*') if p.is_file())
    require(current_claims == plan['claim_paths'], 'ENTRY_OR_RECOVERY_CLAIM_SET_CHANGED')


class ReadOnlyLedger:
    def __init__(self, account, snapshot):
        self.account_fingerprint = account
        self._pending = tuple(row for row in snapshot.reservations if row.status in OPEN)

    def pending_reservations(self):
        return self._pending


class ReadOnlyBroker:
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


def verify_native_changes(before, after, initial, final, evidence, portfolio_evidence):
    """Verify exact cumulative fills, terminal state, and native assignment release."""
    expected = {item.reservation_id: item for item in evidence}
    original_rows = {row['id']: row for row in before['reservations']}
    actual_rows = {row['id']: row for row in after['reservations']}
    require(set(original_rows) == set(actual_rows), 'RESERVATION_IDENTITIES_CHANGED')
    for identity, old in original_rows.items():
        wanted = dict(old)
        if identity in expected:
            item = expected[identity]
            wanted.update(status=item.status, broker_order=item.broker_order_id,
                          filled=item.cumulative_filled_quantity, last_evidence_at=item.observed_at)
        require(actual_rows[identity] == wanted, 'UNEXPECTED_RESERVATION_CHANGE')
    old_fills = {row['id']: row for row in before['fills']}
    wanted_fills = dict(old_fills)
    for item in evidence:
        for fill in item.fills:
            row = {'id': fill.fill_id, 'reservation': item.reservation_id, 'quantity': fill.quantity,
                   'price': str(Decimal(str(fill.price))), 'executed_at': fill.executed_at}
            require(fill.fill_id not in old_fills or old_fills[fill.fill_id] == row, 'OLD_FILL_CHANGED')
            wanted_fills[fill.fill_id] = row
    require({row['id']: row for row in after['fills']} == wanted_fills, 'FILL_DELTA_NOT_EXACT_EVIDENCE')
    require(before.get('inventory_assignments', []) == after.get('inventory_assignments', []), 'ASSIGNMENTS_CHANGED')
    assignments = {row['id']: row for row in before.get('inventory_assignments', [])}
    releases = {row['id']: row for row in before.get('inventory_assignment_releases', [])}
    wanted_releases = dict(releases)
    latest_ready = sorted((row for row in before['snapshots'] if row['ready'] == 1), key=lambda row: row['observed_at'])[-1]
    for identity, item in expected.items():
        old = original_rows[identity]
        assignment_id = _identity(['gameplan-observed-opening-stock',old['idempotency_key']])
        assignment = assignments.get(assignment_id)
        if old['side'] != 'SELL' or assignment is None:
            continue
        released = assignment['quantity'] - max(0, item.cumulative_filled_quantity - (old['quantity']-assignment['quantity']))
        if released > 0:
            release_id = _identity(['gameplan-unsold-opening-stock-release',identity])
            wanted_releases.setdefault(release_id, {'id': release_id, 'assignment': assignment_id,
                'reservation': identity, 'snapshot_id': latest_ready['id'], 'quantity': released,
                'observed_at': item.observed_at})
    require({row['id']: row for row in after.get('inventory_assignment_releases', [])} == wanted_releases,
            'UNEXPECTED_ASSIGNMENT_RELEASE')
    old_allocations = {row.allocation_id: row for row in initial.allocations}
    new_allocations = {row.allocation_id: row for row in final.allocations}
    require(set(old_allocations) == set(new_allocations), 'ALLOCATION_IDENTITIES_CHANGED')
    deltas = {identity: 0 for identity in old_allocations}
    for identity, item in expected.items():
        old = original_rows[identity]
        deltas[old['allocation']] += (item.cumulative_filled_quantity-old['filled'])*(1 if old['side']=='BUY' else -1)
    for identity, row in wanted_releases.items():
        if identity not in releases:
            deltas[assignments[row['assignment']]['allocation']] -= row['quantity']
    for identity, old in old_allocations.items():
        new = new_allocations[identity]
        require(new.filled_shares == old.filled_shares+deltas[identity], 'OWNED_SHARE_DELTA_MISMATCH')
        require(new.reserved_buy_shares == 0 and new.reserved_sell_shares == 0, 'REMAINING_ALLOCATION_RESERVATION')
        old_data, new_data = asdict(old), asdict(new)
        for key in ['status','filled_shares','reserved_buy_shares','reserved_sell_shares']:
            old_data.pop(key); new_data.pop(key)
        require(old_data == new_data, 'ALLOCATION_IDENTITY_OR_WINDOWS_CHANGED')
        require(new.status == old.status or (old.status == 'ACTIVE' and new.status == 'CLOSED' and new.filled_shares == 0),
                'UNEXPECTED_ALLOCATION_STATUS_CHANGE')
    for table in before:
        if table not in {'reservations','fills','allocations','snapshots','evidence','inventory_assignment_releases'}:
            require(before[table] == after[table], 'UNRELATED_LEDGER_TABLE_CHANGED')
    require(after['snapshots'][:-1] == before['snapshots'] and len(after['snapshots']) == len(before['snapshots'])+1,
            'PRIOR_SNAPSHOTS_CHANGED')
    require(after['snapshots'][-1]['id'] == portfolio_evidence.snapshot_id and after['snapshots'][-1]['ready'] == 1,
            'NEW_SNAPSHOT_NOT_READY')
    old_events = {row['id']: row for row in before['evidence']}
    new_events = {row['id']: row for row in after['evidence']}
    require(all(new_events.get(identity) == row for identity,row in old_events.items()), 'OLD_AUDIT_EVIDENCE_CHANGED')
    allowed = {item.evidence_id for item in evidence} | {'portfolio:'+portfolio_evidence.snapshot_id} | (set(wanted_releases)-set(releases))
    require(set(new_events)-set(old_events) == allowed, 'UNEXPECTED_AUDIT_EVIDENCE_DELTA')
    return {'new_fills': len(wanted_fills)-len(old_fills), 'new_assignment_releases': len(wanted_releases)-len(releases),
            'owned_share_deltas_by_allocation': {identity: value for identity,value in deltas.items() if value}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--validate-only', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = read(OUT/'reconciliation-plan.json')
    check_owner(plan)
    check_terminal(plan)
    require(list(STOCK_TRADER_SYMBOLS) == plan['symbols'], 'CURRENT_UNIVERSE_CHANGED')
    require(sha(DB) == plan['ledger_sha256'], 'SOURCE_LEDGER_CHANGED')
    if args.validate_only:
        print(json.dumps({'status':'OFFLINE_GUARDS_VERIFIED','broker_calls':0,'production_writes':0}))
        return
    receipt_path = OUT/'native-reconciliation.json'
    backup_path = OUT/'holdings-before-reconciliation.sqlite3'
    rehearsal_path = OUT/'holdings-native-rehearsal.sqlite3'
    require(not any(path.exists() for path in [receipt_path,backup_path,rehearsal_path]), 'RECONCILIATION_ARTIFACT_ALREADY_EXISTS')
    record = {'started_at': utc().isoformat(), 'status':'PREFLIGHT', 'supervision_owner':plan['supervision_owner'],
              'native_reconcile_called':False, 'orders_submitted':0, 'orders_cancelled':0, 'orders_replaced':0,
              'broker_data_http_methods':['GET'], 'execution_budgets':'ZERO'}
    try:
        require(not SESSION_LOCK.exists() and not CYCLE_LOCK.exists(), 'PREEXISTING_TRADER_LOCK_PRESERVED')
        with exclusive_runtime_lock(SESSION_LOCK, process_name='post-close-native-reconciliation-session'):
            require(not CYCLE_LOCK.exists(), 'PREEXISTING_CYCLE_LOCK_PRESERVED')
            with exclusive_runtime_lock(CYCLE_LOCK, process_name='post-close-native-reconciliation'):
                check_owner(plan); check_terminal(plan)
                require(sha(DB) == plan['ledger_sha256'], 'SOURCE_LEDGER_CHANGED')
                with connect(DB) as source:
                    account = dict(source.execute('SELECT key,value FROM metadata'))['account']
                    require(account == plan['account_fingerprint'], 'SAVED_ACCOUNT_CHANGED')
                    initial = HorizonLedger._snapshot(source)
                    require(not initial.persistent_blocks, 'PERSISTENT_OWNERSHIP_BLOCKS')
                    require([asdict(row) for row in initial.reservations if row.status in OPEN] == plan['pending'],
                            'EXACT_PENDING_RESERVATIONS_CHANGED')
                    latest = dict(source.execute('SELECT * FROM snapshots ORDER BY observed_at DESC LIMIT 1').fetchone())
                    require(latest['ready'] == 1 and json.loads(latest['reasons']) == [], 'SAVED_RECONCILIATION_NOT_READY')
                    with sqlite3.connect(backup_path) as backup:
                        source.backup(backup)
                    with sqlite3.connect(rehearsal_path) as rehearsal:
                        source.backup(rehearsal)
                before = table_state(DB)
                record.update(backup_path=str(backup_path),backup_sha256=sha(backup_path),before_logical_sha256=state_hash(before))
                ledger_view = ReadOnlyLedger(account,initial)
                broker = ReadOnlyBroker()
                auth = broker.prepare_read_snapshot()
                require(broker.stable_account_fingerprint() == account, 'BROKER_ACCOUNT_MISMATCH')
                captured = {}
                def capture_snapshot(_observed):
                    check_owner(plan)
                    evidence = capture_order_evidence(broker,ledger_view,account_fingerprint=account,as_of=utc(),observation_clock=utc)
                    require({item.reservation_id for item in evidence} == {row.reservation_id for row in ledger_view.pending_reservations()}, 'EVIDENCE_SET_CHANGED')
                    require(all(item.status in TERMINAL and item.remaining_quantity == 0 for item in evidence),
                            'NONTERMINAL_ORDER_EVIDENCE_PRESERVED')
                    captured['evidence'] = evidence
                    return capture_portfolio_state(broker,observed_at=utc(),parallel=True,
                                                   literal_cash_only=True,use_actual_quote_timestamps=True)
                portfolio,_,capture_metadata = _capture_portfolio_state_with_retry(broker,observed_at=utc(),parallel=True,
                    retry_delay_seconds=3,maximum_retry_seconds=60,maximum_attempts=20,sleep=time.sleep,
                    monotonic=time.monotonic,capture_snapshot=capture_snapshot)
                evidence = captured['evidence']
                broker.verify_read_snapshot(auth)
                broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
                require(broker.stable_account_fingerprint() == account, 'BROKER_ACCOUNT_CHANGED')
                require(portfolio.working_order_count == 0 and not any(portfolio.pending_buy_shares.values())
                        and not any(portfolio.pending_sell_shares.values()), 'CURRENT_BROKER_WORKING_ORDERS_PRESENT')
                require(set(portfolio.held_shares) == set(plan['symbols']) and set(portfolio.quotes) == set(plan['symbols']),
                        'CURRENT_PORTFOLIO_INCOMPLETE')
                require(all(utc(item.observed_at) < utc(portfolio.observed_at) for item in evidence), 'PORTFOLIO_MUST_FOLLOW_ORDER_EVIDENCE')
                previous_held = json.loads(latest['payload'])['held_shares']
                expected_held = {symbol:Decimal(str(value)) for symbol,value in previous_held.items()}
                old_pending = {row.reservation_id:row for row in ledger_view.pending_reservations()}
                for item in evidence:
                    old = old_pending[item.reservation_id]
                    require(item.cumulative_filled_quantity >= old.filled_quantity, 'CUMULATIVE_FILL_REGRESSION')
                    expected_held[old.symbol] += (item.cumulative_filled_quantity-old.filled_quantity)*(1 if old.side=='BUY' else -1)
                require(expected_held == {symbol:Decimal(str(value)) for symbol,value in portfolio.held_shares.items()},
                        'BROKER_HOLDINGS_NOT_EXACT_CONFIRMED_FILL_DELTA')
                pe = PortfolioEvidence(canonical_sha256([account,utc().isoformat(),portfolio.source_fingerprint]),
                    account,portfolio.observed_at,portfolio.held_shares,{s:q.ask for s,q in portfolio.quotes.items()},
                    {s:0 for s in portfolio.held_shares},portfolio.source_fingerprint)
                # Prove complete per-order fill identity and the resulting state on a copy.
                rehearsal_ledger = HorizonLedger(rehearsal_path,account)
                rehearsal_result = rehearsal_ledger.reconcile(pe,order_evidence=evidence)
                rehearsal_final = rehearsal_ledger.snapshot()
                require(rehearsal_result.ready and not rehearsal_result.reasons and not rehearsal_ledger.pending_reservations(),
                        'NATIVE_COPY_REHEARSAL_NOT_READY')
                rehearsed = table_state(rehearsal_path)
                verified_changes = verify_native_changes(before,rehearsed,initial,rehearsal_final,evidence,pe)
                check_owner(plan); check_terminal(plan)
                require(state_hash(table_state(DB)) == state_hash(before), 'PRODUCTION_LEDGER_CHANGED_DURING_CAPTURE')
                require(0 <= (utc()-utc(portfolio.observed_at)).total_seconds() <= 60, 'CURRENT_PORTFOLIO_EVIDENCE_TOO_OLD')
                record.update(broker_state_capture=capture_metadata,order_evidence=[asdict(item) for item in evidence],
                    portfolio=asdict(portfolio),verified_changes=verified_changes,
                    rehearsal_logical_sha256=state_hash(rehearsed),native_reconcile_called=True)
                ledger = HorizonLedger(DB,account)
                result = ledger.reconcile(pe,order_evidence=evidence)
                final = ledger.snapshot()
                actual = table_state(DB)
                require(result.ready and not result.reasons, 'NATIVE_RECONCILIATION_NOT_READY')
                require(actual == rehearsed and final == rehearsal_final, 'PRODUCTION_DIFFERS_FROM_VERIFIED_REHEARSAL')
                require(not ledger.pending_reservations() and not final.persistent_blocks, 'UNRESOLVED_LEDGER_STATE')
                ownership = _ownership(ROOT,plan['symbols'],account,portfolio.held_shares,utc().isoformat())
                require(ownership['safe_for_planning'] is True, 'READ_ONLY_PLANNING_OWNERSHIP_CHECK_FAILED')
                check_terminal(plan)
                record.update(status='RECONCILED',result=asdict(result),after_logical_sha256=state_hash(actual),
                    ownership=ownership,controls_session_and_entry_claims_preserved=True)
    except Exception as exc:
        message = str(exc)
        record.update(status='FAILED',error_type=type(exc).__name__,
            error_code=message if message.replace('_','').isalnum() and message.isupper() else 'INSPECT_CAPTURE_FAILURE')
        raise
    finally:
        record['finished_at'] = utc().isoformat()
        dump(receipt_path,record)
        print(json.dumps({key:record[key] for key in ['status','native_reconcile_called','finished_at']}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not echo raw provider errors, URLs or authentication context.
        raise SystemExit(1) from None
