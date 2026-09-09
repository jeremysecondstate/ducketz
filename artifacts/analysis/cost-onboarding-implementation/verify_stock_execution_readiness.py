"""Read native stock inputs and broker state; never prepare or submit orders."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from app.services.schwab import SchwabSession
from app.ui.rolling_forecast_data import load_forecast_dashboard
from ml.artifacts import file_checksum
from ml.nightly_gameplan import read_current_gameplan
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS, StockTraderPolicy, canonical_sha256, utc
from ml.stock_trader.gameplan import read_gameplan_stock_activation_intent
from ml.stock_trader.horizon_broker import capture_order_evidence
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.stock_trader.independent_runtime import LEDGER_RELATIVE_PATH
from ml.stock_trader.independent_session import _independent_forecast_preflight
from ml.stock_trader.sizing_policy import FIXED_SIZING_POLICY
from ml.stock_trader.state import capture_portfolio_state


def main() -> None:
    root = Path('C:/DATASTORE')
    checked = utc()
    publication = read_current_gameplan(root)
    action_date = checked.tz_convert('America/Los_Angeles').date()
    forecasts = _independent_forecast_preflight(root, action_date=action_date)
    dashboard = load_forecast_dashboard(root / 'ml/nightly-gameplan-latest/run.json')
    activation = read_gameplan_stock_activation_intent(root)
    broker = SchwabSession()
    identity = broker.stable_account_fingerprint()
    ledger = HorizonLedger(root / LEDGER_RELATIVE_PATH, identity)
    evidence = capture_order_evidence(broker, ledger, account_fingerprint=identity,
                                     as_of=utc(), observation_clock=utc)
    portfolio = capture_portfolio_state(broker, observed_at=utc(), parallel=True)
    broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
    identity_unchanged = identity == broker.stable_account_fingerprint()
    policy = StockTraderPolicy()
    snapshot_id = canonical_sha256([identity, portfolio.observed_at, portfolio.source_fingerprint])
    reconciliation = ledger.reconcile(PortfolioEvidence(
        snapshot_id, identity, portfolio.observed_at, portfolio.held_shares,
        {s: q.ask for s, q in portfolio.quotes.items()},
        {s: portfolio.account_equity * policy.maximum_symbol_equity_fraction for s in portfolio.held_shares},
        portfolio.source_fingerprint,
    ), order_evidence=evidence)
    state = ledger.snapshot()
    automation_path = Path.home() / '.codex/automations/loops-stock-trader-live-shadow/automation.toml'
    automation = tomllib.loads(automation_path.read_text(encoding='utf-8'))
    production_symbols = tuple(line.strip() for line in (REPO / 'datafetching/watchlist.txt').read_text().splitlines()
                               if line.strip() and not line.lstrip().startswith('#'))
    checks = {
        'all_seven_production_symbols': production_symbols == tuple(STOCK_TRADER_SYMBOLS),
        'dashboard_seven_active_symbols_no_pending_badge':
            {s.symbol for s in dashboard.symbols} == set(STOCK_TRADER_SYMBOLS)
            and not dashboard.pending_symbols and dashboard.source_row_count == 168,
        'both_persistent_controls_active': activation.active,
        'all_133_stock_execution_windows_qualified': forecasts.get('status') == 'READY'
            and forecasts.get('all_execution_windows_qualified') is True
            and forecasts.get('execution_window_count') == 133,
        'stable_broker_account_identity': identity_unchanged,
        'seven_native_quote_inputs_available': set(portfolio.quotes) == set(STOCK_TRADER_SYMBOLS),
        'positive_cash_and_equity': portfolio.account_equity > 0 and portfolio.available_cash > 0,
        'native_horizon_ledger_reconciled': reconciliation.ready,
        'live_session_automation_active': automation['status'] == 'ACTIVE',
        'session_start_0355_weekdays': automation['rrule'] == 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=3;BYMINUTE=55',
        'explicit_fixed_budget_native_worker': '--execute --target-horizon all --sizing-policy fixed-horizon-budget-v1 --run-session' in automation['prompt'],
    }
    result = {
        'checked_at': utc().isoformat(), 'action_date': action_date.isoformat(),
        'status': 'READY' if all(checks.values()) else 'NOT_READY', 'checks': checks,
        'mode': 'REAL_BROKER_READ_ONLY_AND_NATIVE_LEDGER_RECONCILIATION',
        'orders_submitted': 0, 'orders_cancelled': 0,
        'sizing_policy': FIXED_SIZING_POLICY,
        'learned_sizing_status': 'RESEARCH_NOT_REQUIRED_BY_SELECTED_FIXED_BUDGET_STRATEGY',
        'forecast_run': str(publication.run_directory),
        'forecast_manifest_sha256': file_checksum(publication.run_directory / 'manifest.json'),
        'forecast_readiness': forecasts,
        'dashboard': {'published_symbols': [s.symbol for s in dashboard.symbols],
                      'pending_symbols': [s.symbol for s in dashboard.pending_symbols],
                      'forecast_rows': dashboard.source_row_count},
        'broker': {'observed_at': portfolio.observed_at, 'quote_symbols': sorted(portfolio.quotes),
                   'native_reconciliation_ready': reconciliation.ready, 'reasons': list(reconciliation.reasons),
                   'tracked_allocations': len(state.allocations), 'order_evidence_count': len(evidence),
                   'manual_shares_not_adopted': len(state.allocations) == 0},
        'automation': {'id': automation['id'], 'status': automation['status'], 'rrule': automation['rrule'],
                       'config_sha256': file_checksum(automation_path)},
        'no_bullish_entry_signal': not forecasts.get('bullish_entry_windows'),
        'interpretation': 'Ready to evaluate and execute qualifying long-stock entries; current frozen bearish/neutral forecasts require no new buys. Broker inputs and order gates are refreshed at each live decision.',
    }
    output = Path(__file__).with_name('stock-execution-ready-20260908.json')
    output.write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'checks': checks, 'evidence': str(output)}, sort_keys=True))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
