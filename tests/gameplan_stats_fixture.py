"""Small saved-review fixtures with real artifact integrity metadata."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml.artifacts import file_checksum, write_manifest


def forecast(symbol="AAPL", hour=4, *, direction="BEARISH", change=-.01, probability=.2,
             status="EVALUATED", horizon="1h", role="EXECUTION", session="2026-09-11"):
    start = pd.Timestamp(session, tz="America/Los_Angeles") + pd.Timedelta(hours=hour)
    end = start + pd.Timedelta(hours={"1h": 1, "4h": 4, "1d": 13, "1w": 120}[horizon])
    evaluated = status == "EVALUATED"
    target = int(change > .001) if evaluated else None
    correct = (change > 0 if direction == "BULLISH" else change < 0) if evaluated and direction != "NO_EDGE" else None
    route = f"{horizon}@{hour:02d}:00" if role == "EXECUTION" else "1h@gap"
    return {"id": f"{session}/{symbol}/{route}", "symbol": symbol, "route": route,
            "action_date": session, "model_group": horizon, "model_status": "PROMOTED",
            "target_role": role, "target_window_start": start, "target_window_end": end,
            "direction": direction, "actuals_status": status, "direction_correct": correct,
            "calibrated_probability": probability, "model_observed_target": target,
            "model_brier_score": (probability-target)**2 if evaluated else None,
            "actual_return": change if evaluated else None,
            "actual_start_observed_at": start.isoformat(),
            "actual_end_observed_at": end.isoformat() if evaluated else None}


def write_review(root: Path, rows=None, *, session="2026-09-11", version="01", latest=True):
    if rows is None:
        rows = [forecast(session=session),
                forecast(hour=5, direction="BULLISH", change=-.02, probability=.8, session=session),
                forecast(hour=6, direction="NO_EDGE", change=.005, probability=.5, session=session),
                forecast(hour=7, status="MATURE_AWAITING_DATA", session=session),
                forecast(hour=8, status="PENDING_MATURITY", session=session),
                forecast(hour=4, horizon="4h", change=.02, probability=.2, session=session),
                forecast("NVDA", probability=.1, session=session)]
    run = root / f"ml/gameplan-actuals-review-runs/{session}-{version}"
    run.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(run / "forecast-results.parquet", index=False)
    report = {"schema_version": "gameplan-actuals-review-v1", "status": "COMPLETE", "action_date": session,
              "reviewed_at": f"{session}T22:15:00-07:00", "outcomes_through": f"{session}T17:00:00-07:00"}
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run / "Gameplan-results.md").write_text(f"# Verified results {session}\n", encoding="utf-8")
    write_manifest(run, run_timestamp=report["reviewed_at"], input_files=[],
                   output_files=["forecast-results.parquet", "report.json", "Gameplan-results.md"],
                   configuration={"schema_version": report["schema_version"], "action_date": session})
    receipt = {"schema_version": report["schema_version"], "status": "COMPLETE", "action_date": session,
               "run_path": run.relative_to(root).as_posix(), "manifest_sha256": file_checksum(run / "manifest.json")}
    (run / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    pointer = {"schema_version": report["schema_version"], "current": {
        "run_path": receipt["run_path"], "action_date": session, "receipt_sha256": file_checksum(run / "receipt.json")}}
    paths = [root / f"ml/gameplan-actuals-review-by-date/{session}/run.json"]
    if latest:
        paths.append(root / "ml/gameplan-actuals-review-latest/run.json")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pointer), encoding="utf-8")
    return run
