"""Offline check of the approved bounded policy against the pinned failed plan."""
import json
import sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path.cwd()))
from ml.artifacts import file_checksum
from ml.stock_target_prices import load_stock_target_prices
from ml.gameplan_price_bands import build_entry_price_bands, build_planning_price_path
from ml.gameplan_trade_planning import _plan_working_price_rows
from ml.gameplan_cash_ledger import project_direction_trades
from ml.stock_trader.contracts import StockTraderPolicy

root = Path('C:/DATASTORE')
source = root/'ml/nightly-gameplan-runs/20260911T051120.445494Z'
failed = root/'ml/gameplan-trade-plan-runs/20260911T051225.122443Z'
out = Path(__file__).resolve().parent
receipt_sha = file_checksum(source/'receipt.json')
assert receipt_sha == '950809362b98ad46853cd5c715829d2f49d39e4eb045986fe7b729c0f5ae6bb9'
forecasts = pd.read_parquet(source/'forecasts.parquet')
snapshot = json.loads((failed/'account-snapshot.json').read_text())
prices, files, inventory = load_stock_target_prices(root, symbols=tuple(forecasts.symbol.unique()), source_contract='xnas-itch-archive-v1')
now = pd.Timestamp.now(tz='UTC')
bands = build_entry_price_bands(prices, forecasts, observed_at=now, allow_reference_forward_fill=True)
path = build_planning_price_path(prices, forecasts, observed_at=now, entry_bands=bands, allow_reference_forward_fill=True)
cost = bands['reference_completion']['references']['COST|2026-09-11']
assert cost['status'] == 'AVAILABLE_SYNTHETIC' and cost['fill_count'] == 13
assert cost['observed_at'] == '2026-09-10T23:47:00+00:00'
assert cost['effective_at'] == '2026-09-11T00:00:00+00:00' and cost['price'] == 902.2
assert len(path['points']) == 98 and all(p['status']=='AVAILABLE' for p in path['points'].values())
policy = StockTraderPolicy()
rows = _plan_working_price_rows(forecasts, snapshot, bands, path, policy=policy)
rows, ledger = project_direction_trades(rows, snapshot, path, policy=policy)
assert len(rows) == 168
assert file_checksum(source/'receipt.json') == receipt_sha
report = {'verified_at': now.isoformat(), 'source_receipt_sha256': receipt_sha,
          'status': 'OFFLINE_PROJECTION_PASSED', 'uses_saved_snapshot': True,
          'reference_completion': bands['reference_completion'], 'planning_points': len(path['points']),
          'available_points': sum(p['status']=='AVAILABLE' for p in path['points'].values()),
          'trade_plan_rows': len(rows), 'ledger': ledger,
          'orders_placed': 0, 'production_publication': False}
(out/'forward-fill-reproduction.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
print(json.dumps({key:value for key,value in report.items() if key not in {'reference_completion','ledger'}}, indent=2))
print(json.dumps(cost, indent=2))
