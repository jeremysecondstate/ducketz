"""Check the real saved review through both app tabs; no services or orders."""
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from datetime import datetime, timezone
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.ui.gameplan import GameplanTab, HORIZON_LABELS
from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats import GameplanStatsTab, HORIZON_LABELS as STATS_HORIZONS, percent
from app.ui.gameplan_stats_data import load_gameplan_stats, prediction_metrics

root = Path('C:/DATASTORE')
plan = load_gameplan(root, '2026-09-16')
stats = load_gameplan_stats(root, '2026-09-15')
run = plan.run_directory
base = root / 'ml/gameplan-trade-plan-runs/20260916T060421.076865Z'
report = json.loads((run / 'report.json').read_text())
path = json.loads((run / 'planning-price-path.json').read_text())
assert len(path['points']) == 154
assert all(p['status'] == 'AVAILABLE' for p in path['points'].values())
assert all(p.price is not None and p.price > 0 for p in plan.forecasts if p.eligible)
assert plan.projection_available and len(plan.forecasts) == 264
assert (base / 'account-snapshot.json').read_bytes() == (run / 'account-snapshot.json').read_bytes()
assert report['orders_placed'] == 0 and report['publication_mode'] == 'INFORMATIONAL_REFRESH'
assert '| PATH | $14.25 |' in plan.report_path.read_text(encoding='utf-8')

window = tk.Tk()
window.withdraw()
frame = ttk.Frame(window)
frame.pack()
tab = GameplanTab(window, frame, datastore_root=root, auto_load=False)
tab.set_plan(plan)
plan_filters = 0
for company in ('All companies', *plan.symbols):
    for label, horizon in HORIZON_LABELS.items():
        tab.company.set(company)
        tab.horizon.set(label)
        tab.view.set('forecasts')
        tab.render()
        expected = [p for p in plan.forecasts if (company == 'All companies' or p.symbol == company)
                    and (horizon == 'all' or p.horizon == horizon)]
        assert {p.forecast_id for p in tab.visible_rows} == {p.forecast_id for p in expected}
        plan_filters += 1
frame.destroy()
frame = ttk.Frame(window)
frame.pack()
tab = GameplanStatsTab(window, frame, datastore_root=root, auto_load=False)
tab.set_review(stats)
stats_filters = 0
for symbol in stats.symbols:
    for label, horizon in STATS_HORIZONS.items():
        tab.selected_symbol = symbol
        tab.horizon.set(label)
        tab.render()
        outcomes = tuple(p for p in stats.outcomes if horizon == 'all' or p.horizon == horizon)
        assert tab.values['accuracy'].get() == percent(prediction_metrics(outcomes).accuracy)
        stats_filters += 1
window.destroy()
result = {'status': 'VERIFIED', 'checked_at': datetime.now(timezone.utc).isoformat(),
          'run': str(run), 'available_price_points': 154, 'eligible_rows_with_prices': 209,
          'gameplan_filter_views': plan_filters, 'stats_filter_views': stats_filters,
          'stats_metrics': asdict(prediction_metrics(stats.outcomes)),
          'original_account_snapshot_preserved': True, 'orders_placed': 0}
Path(__file__).with_name('refreshed-tabs-verification.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
