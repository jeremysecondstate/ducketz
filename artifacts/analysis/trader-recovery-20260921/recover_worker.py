"""Bounded operator repair: cooperative stop, native reconciliation, restore intent.

No order submission/cancellation/replacement APIs, direct ledger edits or lock deletion.
Each phase is invoked separately after inspecting the previous receipt.
"""
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import psutil

sys.path.insert(0, 'C:/dev/ducketz')
from ml.stock_trader.gameplan import gameplan_stock_activation_path, read_gameplan_stock_activation_intent, write_gameplan_stock_activation_intent
from ml.stock_trader.control import operator_intent_path
from ml.stock_trader.contracts import canonical_sha256, utc

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
TOKEN = 'f85c1328-b354-4404-a158-34284ed5a476'
STATUS = ROOT / 'state/independent-stock-trader/session-status.json'
DB = ROOT / 'state/independent-stock-trader/holdings.sqlite3'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str) + '\n', encoding='utf-8')


def check_owner():
    lease = read(ROOT / 'ml/overnight-supervision.json')
    assert lease['owner_token'] == TOKEN
    assert datetime.fromisoformat(lease['expires_at']) > datetime.now(timezone.utc)


def claims():
    return {p.relative_to(ROOT).as_posix(): canonical_sha256(p.read_text())
            for folder in ('entry-slots', 'quote-recovery-slots')
            for p in (ROOT / 'state/independent-stock-trader' / folder).rglob('*') if p.is_file()}


def identities():
    status = read(STATUS)
    worker = psutil.Process(status['pid'])
    launcher = worker.parent()
    result = []
    for process in (launcher, worker):
        command = process.cmdline()
        assert 'ml.gameplan_stock_trader' in command
        assert '--execute' in command and '--run-session' in command and '--wait-for-open' in command
        assert command[command.index('--target-horizon') + 1] == 'all'
        assert command[command.index('--sizing-policy') + 1] == 'gameplan-direction-current-market-v1'
        assert '--late-opening-date' not in command and '--resume-quote-run' not in command
        result.append({'pid': process.pid, 'created_at': process.create_time(), 'command': command,
                       'executable': process.exe(), 'parent_pid': process.ppid()})
    lock = dict(line.split('=', 1) for line in (ROOT / 'locks/independent-stock-session.lock').read_text().splitlines())
    assert int(lock['pid']) == worker.pid
    assert status['action_date'] == '2026-09-21'
    assert (utc() - utc(status['heartbeat_at'])).total_seconds() < 150
    return status, result


def assert_stopped(before):
    status = read(STATUS)
    assert status['pid'] == before['status']['pid'] and status['status'] == 'STOPPED_TRADER_INACTIVE'
    for identity in before['processes']:
        try:
            process = psutil.Process(identity['pid'])
        except psutil.NoSuchProcess:
            continue
        assert process.create_time() != identity['created_at'], 'Original process still alive'
    assert not (ROOT / 'locks/independent-stock-session.lock').exists()
    assert not (ROOT / 'locks/stock-trader-hourly.lock').exists()
    return status


check_owner()
phase = sys.argv[1]
gameplan_control = gameplan_stock_activation_path(ROOT)
global_control = operator_intent_path(ROOT)
if phase == 'stop':
    assert not (OUT / 'before-stop.json').exists(), 'Do not overwrite original restart evidence'
    status, processes = identities()
    assert read_gameplan_stock_activation_intent(ROOT).active
    before = {'observed_at': utc().isoformat(), 'status': status, 'processes': processes,
              'gameplan_control': gameplan_control.read_text(), 'global_control': global_control.read_text(),
              'claims': claims()}
    save('before-stop.json', before)
    write_gameplan_stock_activation_intent(ROOT, active=False)
    save('stop-request.json', {'at': utc().isoformat(), 'method': 'NATIVE_GAMEPLAN_INTENT_FALSE_FOR_COOPERATIVE_RESTART'})
    print('COOPERATIVE_STOP_REQUESTED')
elif phase == 'reconcile':
    before = read(OUT / 'before-stop.json')
    terminal = assert_stopped(before)
    assert global_control.read_text() == before['global_control']
    assert not read_gameplan_stock_activation_intent(ROOT).active
    from app.services.schwab import SchwabSession
    from datafetching.runtime_lock import exclusive_runtime_lock
    from ml.stock_trader.horizon_broker import capture_order_evidence
    from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
    from ml.stock_trader.state import capture_portfolio_state
    from ml.stock_trader.runtime import _capture_portfolio_state_with_retry
    with exclusive_runtime_lock(ROOT / 'locks/stock-trader-hourly.lock', process_name='authorized-trader-reconciliation'):
        if not (OUT / 'holdings-before.sqlite3').exists():
            with sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(OUT / 'holdings-before.sqlite3') as backup:
                    source.backup(backup)
        broker = SchwabSession()
        auth = broker.prepare_read_snapshot()
        identity = broker.stable_account_fingerprint()
        ledger = HorizonLedger(DB, identity)
        original = ledger.snapshot()
        captured = {}
        def capture_snapshot(_timestamp):
            attempt_evidence = capture_order_evidence(broker, ledger, account_fingerprint=identity,
                                                      as_of=utc(), observation_clock=utc)
            attempt_portfolio = capture_portfolio_state(broker, observed_at=utc(), parallel=True,
                literal_cash_only=True, use_actual_quote_timestamps=True)
            captured['evidence'] = attempt_evidence
            return attempt_portfolio
        portfolio, _, capture_metadata = _capture_portfolio_state_with_retry(broker, observed_at=utc(),
            parallel=True, retry_delay_seconds=3, maximum_retry_seconds=120, maximum_attempts=40,
            sleep=time.sleep, monotonic=time.monotonic, capture_snapshot=capture_snapshot)
        evidence = captured['evidence']
        broker.verify_read_snapshot(auth)
        broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
        assert broker.stable_account_fingerprint() == identity
        assert 0 <= (utc() - utc(portfolio.observed_at)).total_seconds() <= ledger.maximum_evidence_age_seconds
        assert len(evidence) == 1
        reservation = original.reservations[0] if len(original.reservations) == 1 else None
        pending = ledger.pending_reservations()
        assert len(pending) == 1 and pending[0].symbol == 'CROX' and pending[0].horizon == '1w'
        assert pending[0].side == 'BUY' and pending[0].quantity == 52 and pending[0].filled_quantity == 0
        assert evidence[0].status == 'CANCELLED' and evidence[0].cumulative_filled_quantity == 0
        assert evidence[0].remaining_quantity == 0 and not evidence[0].fills
        assert portfolio.working_order_count == 0
        assert not original.persistent_blocks
        snapshot_id = canonical_sha256([identity, utc().isoformat(), portfolio.source_fingerprint])
        result = ledger.reconcile(PortfolioEvidence(snapshot_id, identity, portfolio.observed_at,
            portfolio.held_shares, {s: q.ask for s, q in portfolio.quotes.items()},
            {s: 0 for s in portfolio.held_shares}, portfolio.source_fingerprint), order_evidence=evidence)
        after = ledger.snapshot()
        assert {a.allocation_id: a.filled_shares for a in original.allocations} == {a.allocation_id: a.filled_shares for a in after.allocations}
        assert result.ready, result.reasons
        assert not ledger.pending_reservations()
        assert claims() == before['claims']
        save('native-reconciliation.json', {'observed_at': utc().isoformat(), 'terminal_status': terminal,
            'result': asdict(result), 'broker_state_capture': capture_metadata,
            'order_evidence': [asdict(e) for e in evidence],
            'before': asdict(original), 'after': asdict(after), 'portfolio': asdict(portfolio),
            'orders_submitted': 0, 'orders_cancelled': 0, 'orders_replaced': 0,
            'filled_allocations_preserved': True, 'entry_claims_preserved': True})
        print(json.dumps({'status': 'RECONCILED', 'ready': result.ready, 'reasons': result.reasons,
                          'pending_reservations': len(ledger.pending_reservations())}))
elif phase == 'restore':
    before = read(OUT / 'before-stop.json')
    assert_stopped(before)
    assert read(OUT / 'native-reconciliation.json')['result']['ready']
    assert global_control.read_text() == before['global_control']
    assert gameplan_control.read_text() == 'CONFIRM_GAMEPLAN_STOCK_TRADING=FALSE\n'
    assert claims() == before['claims']
    write_gameplan_stock_activation_intent(ROOT, active=True)
    assert gameplan_control.read_text() == before['gameplan_control']
    assert read_gameplan_stock_activation_intent(ROOT).active
    save('restored-intent.json', {'at': utc().isoformat(), 'original_control_contents_restored': True})
    print('ORIGINAL_ACTIVE_INTENT_RESTORED')
else:
    raise ValueError('Unknown maintenance phase')
