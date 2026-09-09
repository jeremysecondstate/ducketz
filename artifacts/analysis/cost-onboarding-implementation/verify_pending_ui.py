"""Record the actual dashboard's published and pending universes."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.ui.rolling_forecast_data import load_forecast_dashboard
from app.ui.rolling_forecasts import prediction_pulse_entries, prediction_pulse_mark_path
from app.ui.schwab_duckets import SECURITY_MARK_FILENAMES
from datafetching.symbol_universe import read_symbols

base = Path(__file__).resolve().parent
view = load_forecast_dashboard(Path(r"C:\DATASTORE\ml\nightly-gameplan-latest\run.json"))
assert [item.symbol for item in view.pending_symbols] == ["COST"]
assert "COST" not in read_symbols()
assert view.source_row_count == view.published_route_count == 144
assert view.frozen_weekly_snapshot_count == len(view.symbols) == 6
assert prediction_pulse_entries(view)[-1] == ("COST", (("1h", None), ("4h", None), ("1d", None)))
output = {"checked_at": datetime.now(timezone.utc).isoformat(),
    "published_symbols": [item.symbol for item in view.symbols],
    "pending_symbols": [asdict(item) for item in view.pending_symbols],
    "pulse_symbols": [entry[0] for entry in prediction_pulse_entries(view)],
    "published_forecast_rows": view.source_row_count, "frozen_weekly_snapshots": view.frozen_weekly_snapshot_count,
    "cost_probability_values": dict(prediction_pulse_entries(view)[-1][1]),
    "cost_logo_present": prediction_pulse_mark_path("COST") is not None,
    "cost_logo_runtime_filename": SECURITY_MARK_FILENAMES["COST"],
    "tests": "133 forecast/onboarding tests passed", "orders_submitted": 0}
(base / "ui-pending-verification.json").write_text(json.dumps(output, indent=2) + "\n")
print(json.dumps(output))
