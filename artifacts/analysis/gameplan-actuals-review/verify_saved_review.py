"""Read-only historical cross-check plus today's explicitly pending preview."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp, verify_manifest
from ml.gameplan_actuals_review import (
    _latest_saved_gameplan, _saved_trade_plan, compare_forecasts,
    compare_price_points, render_actuals_review,
)
from ml.gameplan_evaluation import read_evaluation_history
from ml.stock_target_prices import load_stock_target_prices


root = Path(r"C:\DATASTORE")
output = Path(__file__).resolve().parent
today = _latest_saved_gameplan(root, "2026-09-09")
trades = _saved_trade_plan(root, today)
assert trades is not None
frozen_files = [today.run_directory / "receipt.json", today.run_directory / "forecasts.parquet",
                trades[0] / "receipt.json", trades[0] / "trade-plan.parquet", trades[0] / "planning-price-path.json"]
before = {str(path): file_checksum(path) for path in frozen_files}
data = pd.read_parquet(today.run_directory / "forecasts.parquet")
contract = today.manifest["configuration"]["target_price_source_contract"]
prices, _, inventory = load_stock_target_prices(root, symbols=tuple(sorted(data.symbol.unique())), source_contract=contract)
observed = utc_timestamp()
results = compare_forecasts(data, prices, observed_at=observed, trade_rows=trades[1])
clocks = compare_price_points(data, prices, observed_at=observed, action_date="2026-09-09", planning_path=trades[2])
counts = {"total": len(results), "evaluated": int(results.actuals_status.eq("EVALUATED").sum()),
          "pending_maturity": int(results.actuals_status.eq("PENDING_MATURITY").sum()),
          "mature_awaiting_data": int(results.actuals_status.eq("MATURE_AWAITING_DATA").sum())}
report = {"preview": True, "action_date": "2026-09-09", "successor_action_date": "2026-09-10", "forecasts": counts,
          "source_gameplan_run": today.run_directory.relative_to(root).as_posix(),
          "source_gameplan_path": today.run_directory.as_posix(), "source_trade_plan_path": trades[0].as_posix()}
(output / "Gameplan-results-preview.md").write_text(render_actuals_review(results, clocks, report), encoding="utf-8")

prior = _latest_saved_gameplan(root, "2026-09-08")
assert prior.manifest["configuration"]["target_price_source_contract"] == contract
prior_data = pd.read_parquet(prior.run_directory / "forecasts.parquet")
historical = compare_forecasts(prior_data, prices, observed_at="2026-09-09T00:00Z")
evaluation = read_evaluation_history(root)
saved = evaluation.evaluations.loc[evaluation.evaluations.source_gameplan_run.eq(prior.run_directory.relative_to(root).as_posix())]
matched = historical.merge(saved, left_on="id", right_on="source_forecast_id", suffixes=("", "_evaluation"))
scored = matched.loc[matched.actuals_status.eq("EVALUATED")]
assert len(scored) > 0 and scored.evaluation_status.eq("EVALUATED").all()
assert np.allclose(scored.actual_return.astype(float), scored.observed_return.astype(float), rtol=0, atol=1e-12)
assert np.allclose(scored.model_brier_score.astype(float), scored.brier_score.astype(float), rtol=0, atol=1e-12)
assert set(historical.loc[historical.actuals_status.eq("EVALUATED"), "id"]) == set(saved.loc[saved.evaluation_status.eq("EVALUATED"), "source_forecast_id"])
assert before == {str(path): file_checksum(path) for path in frozen_files}
verify_manifest(trades[0])
receipt = {"checked_at": observed.isoformat(), "status": "VERIFIED", "historical_action_date": "2026-09-08",
           "historical_forecast_rows": len(historical), "real_outcomes_matched_to_existing_evaluator": len(scored),
           "returns_and_brier_scores_agree": True, "today_preview": counts, "hourly_price_rows": len(clocks),
           "saved_forecasts_and_price_estimates_unchanged": True, "price_source": inventory["dataset"],
           "provider_fetches": 0, "broker_calls": 0,
           "preview_path": str(output / "Gameplan-results-preview.md")}
(output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt, indent=2))
