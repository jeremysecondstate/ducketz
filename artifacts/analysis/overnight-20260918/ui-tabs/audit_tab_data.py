"""Read-only audit of the saved data consumed by both native Gameplan tabs."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
import pandas as pd
from app.ui.gameplan_data import HORIZONS, load_gameplan, plan_sessions
from app.ui.gameplan_stats_data import load_gameplan_stats, review_sessions


def metrics(value):
    return asdict(value) | {
        "accuracy": value.accuracy,
        "bullish_accuracy": value.bullish_accuracy,
        "bearish_accuracy": value.bearish_accuracy,
    }


root = Path("C:/DATASTORE")
symbols = sorted(line.strip() for line in (REPO / "datafetching/watchlist.txt").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#"))
plan = load_gameplan(root)
review = load_gameplan_stats(root)
assert plan.session == "2026-09-18", plan.session
assert review.session == "2026-09-17", review.session
assert list(plan.symbols) == symbols == list(review.symbols)
assert len(plan.forecasts) == len(symbols) * 24
assert len({p.forecast_id for p in plan.forecasts}) == len(plan.forecasts)
assert all(len(plan.rows(symbol=symbol)) == 24 for symbol in symbols)
assert plan.projection_available
filter_checks = []
for symbol in (None, *symbols):
    for horizon in ("all", *HORIZONS):
        expected = tuple(row for row in plan.forecasts if (symbol is None or row.symbol == symbol)
                         and (horizon == "all" or row.horizon == horizon))
        assert set(row.forecast_id for row in plan.rows(horizon, symbol)) == set(row.forecast_id for row in expected)
        expected_stats = tuple(row for row in review.outcomes if (symbol is None or row.symbol == symbol)
                               and (horizon == "all" or row.horizon == horizon))
        assert set(row.forecast_id for row in review.rows(horizon, symbol)) == set(row.forecast_id for row in expected_stats)
        filter_checks.append({"symbol": symbol or "all", "horizon": horizon,
                              "plan_rows": len(expected), "stats_rows": len(expected_stats)})

source = pd.read_parquet(review.run_directory / "forecast-results.parquet")
approved = source.loc[source.model_status.eq("PROMOTED")]
assert set(approved.id) == {row.forecast_id for row in review.outcomes}
assert len(source) == len(symbols) * 24
assert review.excluded_forecasts == len(source) - len(approved)
assert all(len(review.hourly(symbol)) == 13 and all(row is not None for row in review.hourly(symbol)) for symbol in symbols)
plan_report = json.loads((plan.run_directory / "report.json").read_text())
path = json.loads((plan.run_directory / "planning-price-path.json").read_text())
assert len(path["points"]) == 14 * len(symbols)
assert all(point["status"] == "AVAILABLE" for point in path["points"].values())
assert plan_report["orders_placed"] == 0

result = {
    "status": "VERIFIED",
    "checked_at": datetime.now(timezone.utc).isoformat(),
    "configured_symbols": symbols,
    "gameplan": {"session": plan.session, "run": str(plan.run_directory), "report": str(plan.report_path),
        "sessions": plan_sessions(root), "rows": len(plan.forecasts),
        "per_horizon": dict(Counter(row.horizon for row in plan.forecasts)),
        "per_symbol": dict(Counter(row.symbol for row in plan.forecasts)),
        "model_statuses": dict(Counter(row.model_status for row in plan.forecasts)),
        "actions": dict(Counter(row.action for row in plan.actions)),
        "entry_windows": sum(row.eligible for row in plan.forecasts),
        "price_points": len(path["points"]), "projection_status": plan.projection_status,
        "planning_note": plan.planning_note},
    "stats": {"session": review.session, "run": str(review.run_directory), "report": str(review.report_path),
        "sessions": review_sessions(root), "source_rows": len(source), "approved_rows": len(review.outcomes),
        "excluded_unpromoted": review.excluded_forecasts,
        "saved_status_counts": {str(k): int(v) for k, v in source.actuals_status.value_counts().items()},
        "saved_model_status_counts": {str(k): int(v) for k, v in source.model_status.value_counts().items()},
        "metrics": metrics(review.metrics()),
        "by_horizon": {horizon: metrics(review.metrics(horizon)) for horizon in HORIZONS},
        "by_symbol": {symbol: metrics(review.metrics(symbol=symbol)) for symbol in symbols},
        "hourly_grid_cells": 13 * len(symbols)},
    "filter_checks": filter_checks,
    "orders_placed": plan_report["orders_placed"],
    "production_files_modified": False,
}
Path(__file__).with_name("tab-data-audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({key: value for key, value in result.items() if key != "filter_checks"}, indent=2))
