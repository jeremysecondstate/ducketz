"""Small plan fixtures with native receipt and manifest integrity metadata."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.ui.gameplan_data import LEDGER_VERSION, VERSION
from ml.artifacts import file_checksum, write_manifest


def forecast(symbol="AAPL", horizon="1d", *, session="2026-09-14", hour=4, action="BUY",
             quantity=17, probability=.5436, role="EXECUTION", route=None, end=None):
    start = pd.Timestamp(f"{session} {hour:02d}:00", tz="America/Los_Angeles")
    finish = pd.Timestamp(end) if end else start + pd.Timedelta(hours={"1h": 1, "4h": 4, "1d": 13, "1w": 109}[horizon])
    route = route or f"{horizon}@{hour:02d}:00"
    return dict(id=f"{session}:{symbol}:{route}", symbol=symbol, action_date=session, model_group=horizon,
                route=route, target_role=role, execution_eligible=role == "EXECUTION", model_status="PROMOTED",
                calibrated_probability=probability, direction="BULLISH" if probability >= .54 else "BEARISH" if probability <= .46 else "NO_EDGE",
                direction_based_action=action, direction_based_trade_quantity=quantity,
                direction_based_reason="BULLISH_BUY" if action == "BUY" else "NON_ENTRY_CONTEXT" if action == "CONTEXT" else
                "NO_AVAILABLE_SHARES_FOR_THIS_HORIZON" if probability <= .46 else "NEUTRAL",
                target_window_start=start, target_window_end=finish, trade_price_mid=332.49,
                projected_trade_quantity=9999, scheduled_trade_quantity=0)


def plan_payload(session="2026-09-14"):
    tomorrow = (pd.Timestamp(session) + pd.Timedelta(days=1)).date().isoformat()
    friday = (pd.Timestamp(session) + pd.Timedelta(days=4)).date().isoformat()
    daily = forecast(session=session)
    weekly = forecast("NVDA", "1w", session=session, quantity=2, probability=.7,
                      end=f"{friday}T17:00:00-07:00")
    rows = [daily, weekly,
            forecast(horizon="1h", session=session, action="HOLD", quantity=0, probability=.45),
            forecast(horizon="4h", session=session, hour=16, action="HOLD", quantity=0, probability=.45,
                     end=f"{tomorrow}T07:00:00-07:00"),
            forecast("GOOG", "4h", session=session, hour=16, action="HOLD", quantity=0, probability=.5,
                     end=f"{tomorrow}T07:00:00-07:00"),
            forecast(session=session, action="CONTEXT", quantity=None, role="OUTLOOK", route="1d@D+2"),
            forecast(horizon="1h", session=session, action="CONTEXT", quantity=None, role="OPENING_GAP_RESEARCH", route="1h@gap")]
    rows[-2]["target_window_start"] += pd.Timedelta(days=1)
    rows[-2]["target_window_end"] += pd.Timedelta(days=1)
    rows[-1]["target_window_start"] -= pd.Timedelta(days=1)
    rows[-1]["target_window_end"] = daily["target_window_start"]
    def event(sequence, row, action, when, price, reason):
        return dict(sequence=sequence, timestamp=when.isoformat(), symbol=row["symbol"], horizon=row["model_group"],
                    forecast_id=row["id"], action=action, quantity=row["direction_based_trade_quantity"],
                    price_base=price, reason=reason)
    ledger = dict(version=LEDGER_VERSION, status="COMPLETE", events=[
        event(1, daily, "BUY", daily["target_window_start"], 332.49, "BULLISH_BUY"),
        event(2, weekly, "BUY", weekly["target_window_start"], 218.26, "BULLISH_BUY"),
        event(3, daily, "SELL", daily["target_window_end"], 333.66, "HORIZON_EXIT"),
    ], ending_allocations=[
        dict(symbol="NVDA", horizon="1w", quantity=2, reserved=0, end=weekly["target_window_end"].isoformat(), forecast_id=weekly["id"]),
        dict(symbol="GOOG", horizon="4h", quantity=3, reserved=2, end=f"{tomorrow}T07:00:00-07:00", forecast_id="earlier-plan:GOOG:4h"),
    ], summary=dict(trade_events=3, buy_events=2, sell_events=1))
    return rows, ledger


def write_plan(root: Path, *, session="2026-09-14", rows=None, ledger=None, latest=True,
               run_name=None, status="COMPLETE", version=VERSION, report_updates=None):
    default_rows, default_ledger = plan_payload(session)
    rows = default_rows if rows is None else rows
    ledger = default_ledger if ledger is None else ledger
    run = root / "ml/gameplan-trade-plan-runs" / (run_name or f"fixture-{session}")
    run.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        (run/name).write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(rows).to_parquet(run/"trade-plan.parquet", index=False)
    save("direction-ledger.json", ledger)
    observed = (pd.Timestamp(session, tz="America/Los_Angeles") - pd.Timedelta(hours=26)).isoformat()
    completed = (pd.Timestamp(observed) + pd.Timedelta(minutes=1)).isoformat()
    source = "a"*64
    save("report.json", dict(schema_version=version, status=status, action_date=session, observed_at=observed,
                             forecast_rows=len(rows), source_receipt_sha256=source, **(report_updates or {})))
    (run/"Gameplan.md").write_text(f"# {session} Gameplan\nSaved fixture plan.\n", encoding="utf-8")
    write_manifest(run, run_timestamp=observed, input_files=[],
                   output_files=["trade-plan.parquet", "direction-ledger.json", "report.json", "Gameplan.md"],
                   configuration=dict(schema_version=version, action_date=session, source_receipt_sha256=source))
    save("receipt.json", dict(schema_version=version, status=status, action_date=session, completed_at=completed,
                              run_path=run.relative_to(root).as_posix(), forecast_rows=len(rows),
                              source_receipt_sha256=source, manifest_sha256=file_checksum(run/"manifest.json")))
    if latest:
        pointer = root/"ml/gameplan-trade-plan-latest/run.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps(dict(schema_version=version, current=dict(action_date=session,
            run_path=run.relative_to(root).as_posix(), receipt_sha256=file_checksum(run/"receipt.json"),
            source_receipt_sha256=source))), encoding="utf-8")
    return run


def unavailable_payload():
    rows, _ = plan_payload()
    for row in rows:
        for key in tuple(row):
            if key.startswith("direction_based_"):
                del row[key]
    ledger = dict(status="UNAVAILABLE_PRICE_REFERENCES", events=[], hourly=[], ending_positions={}, summary={},
                  orders_placed=0, broker_orders_enabled=False, reason="Missing observed price references.",
                  unavailable_points=[dict(symbol="AAPL", reason="Missing exact prior-session close")])
    report = dict(direction_projection_status=ledger["status"], direction_based_projection=ledger)
    return rows, ledger, report
