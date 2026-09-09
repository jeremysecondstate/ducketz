"""Verify the source-bound Sep 9 direction plan and refresh its derived review."""
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_trade_review import render_trade_review


root = Path("C:/DATASTORE")
source = root / "ml/nightly-gameplan-runs/20260909T060404.450224Z"
run = root / "ml/gameplan-trade-plan-runs/20260909T071232.777557Z"
overnight = root / "ml/overnight-runs/20260909T071230.488635Z"
out = Path("C:/dev/ducketz/artifacts/analysis/gameplan-cash-ledger")
review = Path("C:/dev/ducketz/artifacts/gameplans/2026-09-09/Gameplan-2026-09-09-trade-review.md")
expected_source = "bb4f21dc093d1195964e323d0321aa3a10c87e3d1e00cf63bc688df32c3e134b"
assert file_checksum(source / "receipt.json") == expected_source
verify_manifest(source)
verify_manifest(run)
report = json.loads((run / "report.json").read_text())
receipt = json.loads((run / "receipt.json").read_text())
pointer = json.loads((root / "ml/gameplan-trade-plan-latest/run.json").read_text())["current"]
native = json.loads((overnight / "stage-report.json").read_text())
assert native["status"] == receipt["status"] == report["status"] == "COMPLETE"
assert native["stage_order"] == ["gameplan_trade_planning"]
assert native["deadline_at"] == report["deadline_at"] == "2026-09-09T11:00:00+00:00"
assert pointer["run_path"] == run.relative_to(root).as_posix()
assert pointer["receipt_sha256"] == file_checksum(run / "receipt.json")
assert pointer["source_receipt_sha256"] == expected_source
rows = pd.read_parquet(run / "trade-plan.parquet").sort_values("id").reset_index(drop=True)
forecasts = pd.read_parquet(source / "forecasts.parquet").sort_values("id").reset_index(drop=True)
pd.testing.assert_frame_equal(forecasts, rows[forecasts.columns], check_dtype=False)
assert len(rows) == 168 and rows.groupby("symbol").size().eq(24).all()
intents = pd.read_parquet(source / "option-strategy-intents.parquet")
assert set(intents.id) == set(forecasts.id + ":OPTION")
assert intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all()
entries = rows[rows.execution_eligible]
assert len(entries) == 133 and entries.model_status.eq("PROMOTED").all()
assert entries.direction_based_trade_quantity.notna().all()
assert rows.loc[~rows.execution_eligible, "direction_based_trade_quantity"].isna().all()
assert entries.loc[entries.planning_direction.eq("NO_EDGE"), "direction_based_trade_quantity"].eq(0).all()
assert entries.loc[entries.direction_based_trade_quantity.gt(0), "planning_direction"].eq("BULLISH").all()
assert entries.loc[entries.direction_based_trade_quantity.lt(0), "planning_direction"].eq("BEARISH").all()
prices = json.loads((run / "planning-price-path.json").read_text())
assert len(prices["points"]) == 98
assert all(point["status"] == "AVAILABLE" for point in prices["points"].values())
ledger = json.loads((run / "direction-ledger.json").read_text())
assert ledger == report["direction_based_projection"]
cash = [Decimal(str(ledger["summary"]["starting_cash"]))] * 3
shares = {symbol: Decimal(str(q)) for symbol, q in ledger["starting_positions"].items()}
hours = {pd.Timestamp(row["timestamp"]): row for row in ledger["hourly"]}
for clock, hour in hours.items():
    for event in [event for event in ledger["events"] if pd.Timestamp(event["timestamp"]) == clock]:
        sign = 1 if event["action"] == "BUY" else -1
        qty = Decimal(str(event["quantity"]))
        assert shares[event["symbol"]] == Decimal(str(event["shares_before"]))
        shares[event["symbol"]] += qty * sign
        assert shares[event["symbol"]] >= 0
        assert shares[event["symbol"]] == Decimal(str(event["shares_after"]))
        for index, field in enumerate(("low", "base", "high")):
            assert cash[index] == Decimal(str(event[f"cash_before_{field}"]))
            price_field = ("high", "base", "low")[index] if sign == 1 else field
            change = -sign * qty * Decimal(str(event[f"price_{price_field}"]))
            assert change == Decimal(str(event[f"cash_change_{field}"]))
            cash[index] += change
            assert cash[index] == Decimal(str(event[f"cash_{field}"]))
    assert all(cash[i] == Decimal(str(hour[f"cash_{field}"])) for i, field in enumerate(("low", "base", "high")))
    assert all(value == Decimal(str(hour["held_shares"][symbol])) for symbol, value in shares.items())
    at_clock = entries[pd.to_datetime(entries.target_window_start, utc=True).eq(clock)]
    for row in at_clock.to_dict("records"):
        assert all(cash[i] == Decimal(str(row[f"projected_cash_after_{field}"])) for i, field in enumerate(("low", "base", "high")))
        assert shares[row["symbol"]] == Decimal(str(row["projected_shares_after"]))
assert all(cash[i] == Decimal(str(ledger["summary"][f"ending_cash_{field}"])) for i, field in enumerate(("low", "base", "high")))
assert all(shares[s] == Decimal(str(q)) for s, q in ledger["ending_positions"].items())
assert report["orders_placed"] == report["snapshot"]["orders_placed"] == 0
assert report["snapshot"]["broker_data_http_methods"] == ["GET"]

# This is the user-facing derived view. The native publication remains immutable.
models = json.loads((source / "model-reports.json").read_text())
text = render_trade_review(pd.read_parquet(run / "trade-plan.parquet"), report, models, source_gameplan=source.as_posix())
assert text.count("| Direction Based Trade Qty |") == 7
assert text.count("| Cash available after (range) |") == 7
assert "reference only" not in text.lower()
assert "Daily · Day 2 · Sep 09" in text and "Daily · Day 3 · Sep 10" in text
assert "before fees and taxes" in text.lower()
backup = review.with_name(review.stem + "-capacity-v2.md")
if not backup.exists():
    assert file_checksum(review) == "b06f20de89bbbd07faf5a25a2c5e4af21798e617423418ccedc1900a7b3bd910"
    backup.write_bytes(review.read_bytes())
temporary = review.with_suffix(".md.tmp")
temporary.write_text(text, encoding="utf-8")
temporary.replace(review)
result = {"status": "VERIFIED", "source_gameplan_run": source.as_posix(),
          "source_receipt_sha256": expected_source, "trade_plan_run": run.as_posix(),
          "native_overnight_run": overnight.as_posix(), "source_forecasts_preserved": True,
          "source_option_intents": len(intents), "forecast_rows": len(rows), "entry_rows": len(entries),
          "positive_capacity_rows": int(entries.projected_trade_quantity.gt(0).sum()),
          "direction_action_counts": entries.direction_based_action.value_counts().to_dict(),
          "price_path_points": len(prices["points"]), "working_half_width_bps": prices["working_half_width_bps"],
          "minimum_observed_pairs": min(point["sample_count"] for point in prices["points"].values()),
          "cash_and_inventory_reconciled": True, "summary": ledger["summary"],
          "starting_positions": ledger["starting_positions"], "ending_positions": ledger["ending_positions"],
          "broker_snapshot_at": report["snapshot"]["observed_at"], "orders_placed": 0,
          "review_path": review.as_posix(), "review_sha256": file_checksum(review),
          "native_review_sha256": file_checksum(run / "Gameplan.md"),
          "derived_review_note": "Rendered from immutable v3 rows/report with current renderer; native outputs preserved."}
(out / "publication-verification-v3.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
