"""Verify added diagnostics against the frozen Friday review, without publication."""
from pathlib import Path
import json
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import compare_forecasts, compare_price_points, render_actuals_review


ROOT = Path("C:/DATASTORE")
OUT = Path(__file__).resolve().parent
REVIEW = ROOT / "ml/gameplan-actuals-review-runs/20260912T051439.995563Z"
report = json.loads((REVIEW / "report.json").read_text(encoding="utf-8"))
verify_manifest(REVIEW)
source = Path(report["source_gameplan_path"])
trade = Path(report["source_trade_plan_path"])
verify_manifest(source)
verify_manifest(trade)
prices = pd.read_parquet(OUT / "raw-coverage-recent-prices.parquet")
prices.attrs["stock_price_source"] = json.loads(
    (OUT / "raw-coverage-source-inventory.json").read_text(encoding="utf-8")
)
forecasts = pd.read_parquet(source / "forecasts.parquet")
trades = pd.read_parquet(trade / "trade-plan.parquet")
path = json.loads((trade / "planning-price-path.json").read_text(encoding="utf-8"))
new_forecasts = compare_forecasts(
    forecasts, prices, observed_at=report["outcomes_through"], trade_rows=trades
)
new_prices = compare_price_points(
    forecasts, prices, observed_at=report["outcomes_through"],
    action_date=report["action_date"], planning_path=path,
)
old_forecasts = pd.read_parquet(REVIEW / "forecast-results.parquet")
old_prices = pd.read_parquet(REVIEW / "price-results.parquet")

for old, new, keys in (
    (old_forecasts, new_forecasts, ["id"]),
    (old_prices, new_prices, ["symbol", "clock_local"]),
):
    pd.testing.assert_frame_equal(
        old.sort_values(keys).reset_index(drop=True),
        new.loc[:, old.columns].sort_values(keys).reset_index(drop=True),
        check_dtype=False, check_exact=True,
    )

missing = new_prices.loc[new_prices.comparison_status.eq("MATURE_AWAITING_DATA")]
assert set(missing.symbol) == {"COST"}
assert missing.actual_status.eq("OUTSIDE_TOLERANCE").all()
assert missing.actual_source_coverage.eq("VERIFIED_COMPLETE").all()
assert missing.set_index("clock_local").actual_gap_seconds.to_dict() == {
    "14:00": 480.0, "15:00": 840.0, "16:00": 3540.0,
}
for row in new_forecasts.loc[new_forecasts.actuals_status.eq("MATURE_AWAITING_DATA")].to_dict("records"):
    assert row["symbol"] == "COST"
    for side in ("start", "end"):
        if row[f"actual_{side}_status"] == "OUTSIDE_TOLERANCE":
            assert row[f"actual_{side}_source_coverage"] == "VERIFIED_COMPLETE"

pointer_checks = []
for item in json.loads((OUT / "original-pointer-hashes.json").read_text(encoding="utf-8-sig")):
    current = file_checksum(Path(item["path"]))
    assert current.lower() == item["sha256"].lower(), item["path"]
    pointer_checks.append({"path": item["path"], "sha256": current, "unchanged": True})

new_forecasts.to_parquet(OUT / "diagnostic-forecast-results.parquet", index=False)
new_prices.to_parquet(OUT / "diagnostic-price-results.parquet", index=False)
readable = render_actuals_review(new_forecasts, new_prices, report)
readable = "# Offline diagnostic preview; original publication preserved\n\n" + readable
(OUT / "Gameplan-results-diagnostics.md").write_text(readable, encoding="utf-8")
result = {
    "verified_at": pd.Timestamp.now(tz="UTC").isoformat(),
    "status": "VERIFIED_DIAGNOSTICS_ONLY",
    "source_review": str(REVIEW),
    "original_forecast_columns_identical": True,
    "original_price_columns_identical": True,
    "forecast_rows": len(new_forecasts),
    "price_rows": len(new_prices),
    "forecast_statuses": new_forecasts.actuals_status.value_counts().to_dict(),
    "price_statuses": new_prices.comparison_status.value_counts().to_dict(),
    "added_forecast_columns": sorted(set(new_forecasts) - set(old_forecasts)),
    "added_price_columns": sorted(set(new_prices) - set(old_prices)),
    "pointer_checks": pointer_checks,
    "orders_placed": 0,
    "published": False,
}
(OUT / "diagnostic-verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
