"""Read-only source-selection check; never reads broker state or claims slots."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.gameplan_deployment import assert_execution_gameplan, read_deployment
from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals


root = Path('C:/DATASTORE')
deployment = read_deployment(root, '2026-09-18')
assert deployment['status'] == 'ACTIVE'
yg = root / deployment['YG']['run_path']
og = root / deployment['OG']['run_path']
assert_execution_gameplan(root, yg, action_date='2026-09-18')
try:
    assert_execution_gameplan(root, og, action_date='2026-09-18')
except ValueError as exc:
    og_rejection = str(exc)
else:
    raise AssertionError('Frozen OG unexpectedly accepted for execution')
signals, sources = load_current_independent_gameplan_signals(root,
    as_of=pd.Timestamp('2026-09-18T11:01:00Z'), execution_ready_plan=True)
assert signals and all(signal.source_fingerprint == yg.name for signal in signals.values())
stats = load_gameplan_stats(root, '2026-09-17')
metrics = stats.metrics()
assert metrics.correct == 62 and metrics.scored == 164
output = {
    'status': 'VERIFIED', 'checked_at': datetime.now(timezone.utc).isoformat(),
    'action_date': '2026-09-18', 'deployment': deployment,
    'yg_execution_source_accepted': str(yg), 'og_execution_source_rejected': og_rejection,
    'opening_reader_signals': len(signals),
    'opening_reader_source_ids': sorted({signal.source_fingerprint for signal in signals.values()}),
    'reading_only_as_of': '2026-09-18T11:01:00Z',
    'broker_calls': 0, 'orders_placed': 0, 'entry_slots_claimed': 0,
    'preserved_sep17_og_stats': {'correct': metrics.correct, 'scored': metrics.scored, 'brier': metrics.brier},
}
Path(__file__).with_name('active-source-verification.json').write_text(json.dumps(output, indent=2), encoding='utf-8')
print(json.dumps({k:v for k,v in output.items() if k != 'deployment'}, indent=2))
